"""Async PDF fetcher with bounded concurrency, throttling, and retry.

Tenacity handles transient 5xx / network errors with exponential backoff.
The semaphore + per-fetch sleep keep us inside the BOE community-convention
rate limit (deep-dive §8: ~2s on xml.php; we apply the same to PDFs).
"""

from __future__ import annotations

import asyncio

import httpx
import structlog
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

log = structlog.get_logger()


class PDFFetchError(Exception):
    """A PDF fetch failed after all retries."""


class PDFFetcher:
    def __init__(
        self,
        client: httpx.AsyncClient,
        concurrency: int,
        throttle_secs: float,
    ) -> None:
        self._client = client
        self._semaphore = asyncio.Semaphore(concurrency)
        self._throttle_secs = throttle_secs

    async def fetch(self, url: str) -> bytes:
        """Fetch a PDF as bytes. Returns the response body on success.

        Raises `PDFFetchError` on terminal failure after retries.
        """
        try:
            return await self._fetch_with_retry(url)
        except Exception as exc:
            log.warning("pdf_fetch_failed", url=url, error=str(exc))
            raise PDFFetchError(f"failed to fetch {url}: {exc}") from exc

    @retry(
        retry=retry_if_exception_type((httpx.HTTPStatusError, httpx.TransportError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=30),
        reraise=True,
    )
    async def _fetch_with_retry(self, url: str) -> bytes:
        async with self._semaphore:
            resp = await self._client.get(url)
            resp.raise_for_status()
            await asyncio.sleep(self._throttle_secs)
            return resp.content
