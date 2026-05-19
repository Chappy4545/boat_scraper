"""ベーススクレイパー: レート制限・キャッシュ・リトライ・ロギングを一元管理する。"""
import time
from typing import Optional
import requests
from tenacity import retry, stop_after_attempt, wait_fixed, retry_if_exception_type

from src.utils.cache import FileCache
from src.utils.logger import get_logger

logger = get_logger(__name__)


class BaseScraper:
    def __init__(self, config: dict):
        cfg = config["scraping"]
        self.base_url = cfg["base_url"]
        self.delay = float(cfg["delay_seconds"])
        self.timeout = int(cfg["timeout"])
        self.max_retries = int(cfg["max_retries"])
        self.cache = FileCache(cache_dir=cfg["cache_dir"])
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": cfg["user_agent"],
            "Accept-Language": "ja,en;q=0.9",
        })
        self._last_request_time: float = 0.0

    def _throttle(self) -> None:
        elapsed = time.time() - self._last_request_time
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)

    def _fetch_raw(self, url: str, params: Optional[dict] = None) -> str:
        cached = self.cache.get(url, params)
        if cached is not None:
            logger.debug(f"[CACHE HIT] {url} {params}")
            return cached

        self._throttle()
        logger.info(f"[FETCH] {url} params={params}")
        try:
            resp = self._fetch_with_retry(url, params)
        except Exception as e:
            logger.error(f"[FETCH ERROR] {url}: {e}")
            raise
        finally:
            self._last_request_time = time.time()

        html = resp.text
        self.cache.set(url, html, params)
        return html

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_fixed(5),
        retry=retry_if_exception_type(requests.RequestException),
        reraise=True,
    )
    def _fetch_with_retry(self, url: str, params: Optional[dict]) -> requests.Response:
        resp = self._session.get(url, params=params, timeout=self.timeout)
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding or "utf-8"
        return resp

    def close(self) -> None:
        self._session.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
