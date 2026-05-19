"""DB接続・セッション管理・初期化"""
from contextlib import contextmanager
from pathlib import Path
from sqlalchemy import create_engine
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
    logger.info(f"Database initialized: {db_url}")


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
