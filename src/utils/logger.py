import sys
from pathlib import Path
from loguru import logger


def setup_logger(level: str = "INFO", log_dir: str = "logs") -> None:
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    logger.remove()
    logger.add(sys.stderr, level=level, colorize=True,
               format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | <cyan>{name}</cyan> - {message}")
    logger.add(
        Path(log_dir) / "boatrace_{time:YYYY-MM-DD}.log",
        level=level,
        rotation="50 MB",
        retention="14 days",
        compression="zip",
        encoding="utf-8",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {name} - {message}",
    )


def get_logger(name: str):
    return logger.bind(name=name)
