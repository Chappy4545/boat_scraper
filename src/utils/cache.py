"""HTMLレスポンスのファイルキャッシュ。同一URLの重複取得を防ぐ。

⚠️ TTL は一律ではない。ページには2種類ある。

    もう変わらない   過去の日付のページ。結果も払戻も確定している
    まだ変わる       当日のページ。レースが進むたびに中身が増える

2026-08-31 まで一律24時間だったため、**当日のページが半日前の写しで
返っていた**。2026-08-30 の実害:

    16:00 に取得した pay ページ（終了済み121レース）がキャッシュされる
    22:53 の夜間処理が同じ121件を読む（実際は168レース終了済み）
    結果収集が 119/168 で終わり、判定は 16/20 本

さらに悪いことに、キャッシュ削除は処理の**最後**に走る（main._purge_raw_cache）。
途中で落ちた run は古いキャッシュを残すので、**やり直したいときに限って
古い写しを読む**。当日のページを短命にすれば、削除の位置に関係なく直る。
"""
import hashlib
import json
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional


def _write(path: Path, text: str) -> None:
    """改行を変換せずに書く。

    ⚠️ `Path.write_text` は Windows で `\\n` を `\\r\\n` に変換する。読むときは
    universal newlines が `\\r\\n` を `\\n` に戻すので、元が `\\n` だけなら
    往復する。**元が `\\r\\n` だと壊れる**: 書くとき `\\r\\r\\n`、読むとき
    `\\n\\n` になる。2026-08-31 実測で pay ページの 17箇所がこれで化けており、
    「取得したHTML」と「キャッシュのHTML」が別の文字列になっていた。
    パースは今のところ同じ結果だが、キャッシュに当たったかで挙動が変わる
    余地を残してはいけない。
    """
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def _read(path: Path) -> str:
    with open(path, "r", encoding="utf-8", newline="") as f:
        return f.read()


class FileCache:
    """URL+パラメータ単位のHTMLキャッシュ。

    ttl_hours          もう変わらないページの寿命
    live_ttl_minutes   まだ変わるページ（当日・日付不明）の寿命。
                       0 にすると当日のページを一切キャッシュしない
    """

    def __init__(self, cache_dir: str = "data/raw", ttl_hours: int = 24,
                 live_ttl_minutes: int = 5):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.ttl = timedelta(hours=ttl_hours)
        self.live_ttl = timedelta(minutes=live_ttl_minutes)

    # ── 寿命の決め方 ────────────────────────────────
    def _is_settled(self, params: Optional[dict], now: datetime) -> bool:
        """そのページの中身がもう変わらないか。

        公式サイトのページは日付を `hd=YYYYMMDD` で受け取る。過去日なら
        レースは全部終わっており、結果も払戻も動かない。
        `hd` が無い / 読めない / 当日以降なら「まだ変わる」に倒す。
        **判断できないときは短命側**に倒すこと。逆に倒すと、変わるページを
        古いまま返して黙って壊れる（2026-08-30 がこれ）。
        """
        hd = (params or {}).get("hd")
        if not hd:
            return False
        try:
            return datetime.strptime(str(hd), "%Y%m%d").date() < now.date()
        except (ValueError, TypeError):
            return False

    def ttl_for(self, params: Optional[dict], now: Optional[datetime] = None) -> timedelta:
        now = now or datetime.now()
        return self.ttl if self._is_settled(params, now) else self.live_ttl

    # ── 読み書き ────────────────────────────────
    def _key(self, url: str, params: Optional[dict] = None) -> str:
        raw = url + (json.dumps(params, sort_keys=True) if params else "")
        return hashlib.sha256(raw.encode()).hexdigest()

    def _path(self, key: str) -> Path:
        return self.cache_dir / f"{key}.html"

    def _meta_path(self, key: str) -> Path:
        return self.cache_dir / f"{key}.meta.json"

    def get(self, url: str, params: Optional[dict] = None) -> Optional[str]:
        key = self._key(url, params)
        meta_path = self._meta_path(key)
        html_path = self._path(key)
        if not html_path.exists() or not meta_path.exists():
            return None
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            cached_at = datetime.fromisoformat(meta["cached_at"])
        except Exception:
            # メタが壊れていたら「無い」と同じ扱い。取り直す方が安全。
            return None
        now = datetime.now()
        if now - cached_at > self.ttl_for(params, now):
            return None
        return _read(html_path)

    def set(self, url: str, html: str, params: Optional[dict] = None) -> None:
        # 当日のページを保存しない設定（live_ttl=0）なら書かない。
        # 書いてから毎回期限切れにするより、そもそも置かない方が分かりやすい。
        if not self.live_ttl and not self._is_settled(params, datetime.now()):
            return
        key = self._key(url, params)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        _write(self._path(key), html)
        self._meta_path(key).write_text(
            json.dumps({"url": url, "params": params, "cached_at": datetime.now().isoformat()}),
            encoding="utf-8",
        )

    def invalidate(self, url: str, params: Optional[dict] = None) -> None:
        key = self._key(url, params)
        for p in [self._path(key), self._meta_path(key)]:
            if p.exists():
                p.unlink()
