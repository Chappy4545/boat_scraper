"""汎用ヘルパー関数。"""
import yaml
from pathlib import Path
from typing import Any


def load_config(path: str = "configs/config.yaml") -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


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
