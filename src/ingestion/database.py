"""DB接続・セッション管理・初期化"""
from contextlib import contextmanager
from pathlib import Path
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker, Session

from .models import Base
from src.utils.helpers import load_config
from src.utils.logger import get_logger

logger = get_logger(__name__)

_engine = None
_SessionLocal = None


def init_db(config: dict | None = None) -> None:
    global _engine, _SessionLocal
    if config is None:
        config = load_config()
    db_url = config["database"]["url"]
    # SQLite の場合はDBファイルのディレクトリを自動作成
    if db_url.startswith("sqlite"):
        db_path = db_url.replace("sqlite:///", "")
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    _engine = create_engine(
        db_url,
        echo=config["database"].get("echo", False),
        connect_args={"check_same_thread": False} if "sqlite" in db_url else {},
    )
    _SessionLocal = sessionmaker(bind=_engine, autocommit=False, autoflush=False)
    Base.metadata.create_all(_engine)
    _add_missing_columns(_engine)
    logger.info(f"Database initialized: {db_url}")


# モデルに足した列のうち、既存DBに無いもの。
# ⚠️ `create_all` はテーブルを作るだけで、**列は足さない**。だから
# 424MB の履歴DBには新しい列が入らず、書き込みが静かに失敗する
# （saver は例外を握りつぶして warning にするので気づけない）。
# 追加はメタデータ操作なので大きいDBでも一瞬で終わる。
_EXPECTED_COLUMNS: dict[str, dict[str, str]] = {
    "odds": {"odds_upper": "FLOAT"},
}


def _add_missing_columns(engine) -> None:
    """モデルに足した列を既存DBへ反映する（何度実行しても安全）。"""
    insp = inspect(engine)
    for table, cols in _EXPECTED_COLUMNS.items():
        if not insp.has_table(table):
            continue
        have = {c["name"] for c in insp.get_columns(table)}
        for name, ddl in cols.items():
            if name in have:
                continue
            with engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
            logger.info(f"列を追加: {table}.{name} {ddl}")


def get_engine():
    if _engine is None:
        init_db()
    return _engine


@contextmanager
def get_session() -> Session:
    if _SessionLocal is None:
        init_db()
    session: Session = _SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
