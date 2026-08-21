"""汎用ヘルパー関数。"""
import os

import yaml
from pathlib import Path
from typing import Any


def load_config(path: str = "configs/config.yaml") -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    # クラウドで当日予測を回すとき、履歴DB(385MB)は持ち込まない。
    # その日のぶんだけの使い捨てDBを指すために環境変数で差し替える。
    # load_config はあちこちから呼ばれ、そのたびに init_db されるので、
    # 呼び出し側で config を書き換えるだけでは元のDBに戻ってしまう。
    db_url = os.environ.get("BOAT_DB_URL")
    if db_url:
        cfg.setdefault("database", {})["url"] = db_url
    return cfg


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(str(value).replace(",", "").strip())
    except (ValueError, TypeError):
        return default


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).replace(",", "").strip())
    except (ValueError, TypeError):
        return default


def stadium_name(code: str, config: dict) -> str:
    return config.get("stadiums", {}).get(f"{int(code):02d}", f"場{code}")


def date_to_str(date) -> str:
    """datetime.date → 'YYYYMMDD'"""
    return date.strftime("%Y%m%d")
