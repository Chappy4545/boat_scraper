"""異常のときに通知が飛ぶかを見る。

⚠️ **監視は異常時の経路こそ壊れる。** 正常時は動くのに異常時だけ黙る、が
このプロジェクトで一番多い壊れ方だった:

    2026-08-24 watchdog.py が警告文の絵文字で UnicodeEncodeError を起こし、
                print が通知の手前で落ちていた（正常時は動く）
    2026-08-28 夜間処理の未完走が3日間気づかれなかった。daily_check は
                「通知先が設定されているか」を確かめるだけで、**自分では
                一度も送っていなかった**

なので「異常を作って、送信まで到達するか」を見る。
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import daily_check  # noqa: E402

D = date(2026, 8, 31)
NG = [("夜間処理の完走", False, "直近7回中 未完走1回"),
      ("賭式の欠け", False, "1/6賭式　※欠け tansho")]


class _Spy:
    def __init__(self):
        self.sent = []

    def send(self, text):
        self.sent.append(text)


@pytest.fixture
def spy(monkeypatch):
    import notify
    s = _Spy()
    monkeypatch.setattr(notify, "_send", s.send)
    monkeypatch.setattr(notify, "webhook_url", lambda: "https://example.invalid/hook")
    return s


def test_異常があれば通知する(spy):
    assert daily_check.notify_ng(D, NG) is True
    assert len(spy.sent) == 1, "送信が呼ばれていない"
    body = spy.sent[0]
    assert "2026-08-31" in body
    assert "2件" in body
    for name, _ok, detail in NG:
        assert name in body, f"{name} が本文に無い"
        assert detail in body, f"{name} の詳細が本文に無い"


def test_宛先が無ければ送らないが落ちもしない(monkeypatch, spy):
    import notify
    monkeypatch.setattr(notify, "webhook_url", lambda: None)
    assert daily_check.notify_ng(D, NG) is False
    assert spy.sent == []


def test_送信が失敗しても点検自体は落ちない(monkeypatch, spy):
    """notify._send は失敗時に exit(1) する。巻き込まれてはいけない。"""
    import notify

    def boom(_text):
        raise SystemExit(1)

    monkeypatch.setattr(notify, "_send", boom)
    assert daily_check.notify_ng(D, NG) is False


def test_通知本文に絵文字が入っても落ちない(monkeypatch, spy):
    """⚠️ 2026-08-24 の再発防止。cp932 のリダイレクト先で
    UnicodeEncodeError を起こし、print が通知の手前で落ちていた。"""
    ng = [("見込みオッズの縮み", False, "直近7日 1.99 ⚠️ 急に強まっています")]
    assert daily_check.notify_ng(D, ng) is True
    assert "⚠️" in spy.sent[0]
