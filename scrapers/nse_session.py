"""
NSE session manager.

NSE protects its JSON APIs with a browser-fingerprint check:
  1. GET / to seed cookies (nsit, nseappid, etc.)
  2. GET a market-data page to refresh the session
  3. All subsequent API calls carry those cookies

We re-warm the session every 25 requests to avoid 401/403 responses.
"""

import os
import time
import logging
from typing import Any

import requests
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception,
    before_sleep_log,
)

from config import NSE_BASE_URL, NSE_HEADERS, MAX_RETRIES, REQUEST_DELAY_SECONDS

logger = logging.getLogger(__name__)

_WARM_UP_URLS = [
    f"{NSE_BASE_URL}/",
    f"{NSE_BASE_URL}/market-data/all-reports-equities",
    f"{NSE_BASE_URL}/market-data/securities-available-for-trading",
]

SESSION_REFRESH_EVERY = 25

_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, requests.HTTPError):
        return exc.response is not None and exc.response.status_code in _RETRYABLE_STATUS
    return isinstance(exc, (requests.ConnectionError, requests.Timeout))


class NSESession:
    def __init__(self):
        self._session = requests.Session()
        self._session.headers.update(NSE_HEADERS)
        proxy_url = os.environ.get("NSE_PROXY_URL")
        if proxy_url:
            self._session.proxies.update({"http": proxy_url, "https": proxy_url})
            logger.info("NSE session using proxy")
        self._request_count = 0
        self._warm_up()

    def _warm_up(self):
        for url in _WARM_UP_URLS:
            try:
                resp = self._session.get(url, timeout=15)
                resp.raise_for_status()
                time.sleep(0.8)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Warm-up request failed for %s: %s", url, exc)

    @retry(
        retry=retry_if_exception(_is_retryable),
        stop=stop_after_attempt(MAX_RETRIES),
        wait=wait_exponential(multiplier=2, min=2, max=30),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )
    def get(self, url: str, **kwargs) -> requests.Response:
        self._request_count += 1
        if self._request_count % SESSION_REFRESH_EVERY == 0:
            logger.debug("Re-warming NSE session after %d requests", self._request_count)
            self._warm_up()

        time.sleep(REQUEST_DELAY_SECONDS)
        resp = self._session.get(url, timeout=20, **kwargs)

        if resp.status_code in (401, 403):
            logger.warning("Got %d, re-warming session and retrying.", resp.status_code)
            self._warm_up()
            resp = self._session.get(url, timeout=20, **kwargs)

        resp.raise_for_status()
        return resp

    def get_json(self, url: str, **kwargs) -> Any:
        resp = self.get(url, **kwargs)
        if not resp.content or resp.content[:1] not in (b"{", b"["):
            raise ValueError(
                f"Non-JSON response from {url} "
                f"(status={resp.status_code}, body={resp.text[:120]!r})"
            )
        return resp.json()
