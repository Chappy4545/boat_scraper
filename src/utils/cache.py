"""HTMLレスポンスのファイルキャッシュ。同一URLの重複取得を防ぐ。"""
import hashlib
import json
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional


class FileCache:
    def __init__(self, cache_dir: str = "data/raw", ttl_hours: int = 24):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.ttl = timedelta(hours=ttl_hours)

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
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        cached_at = datetime.fromisoformat(meta["cached_at"])
        if datetime.now() - cached_at > self.ttl:
            return None
        return html_path.read_text(encoding="utf-8")

    def set(self, url: str, html: str, params: Optional[dict] = None) -> None:
        key = self._key(url, params)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._path(key).write_text(html, encoding="utf-8")
        self._meta_path(key).write_text(
            json.dumps({"url": url, "params": params, "cached_at": datetime.now().isoformat()}),
            encoding="utf-8",
        )

    def invalidate(self, url: str, params: Optional[dict] = None) -> None:
        key = self._key(url, params)
        for p in [self._path(key), self._meta_path(key)]:
            if p.exists():
                p.unlink()
