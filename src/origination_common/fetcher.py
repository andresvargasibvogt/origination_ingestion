"""Async file fetcher with bounded concurrency, throttling, and retry.

Source-agnostic: fetches whatever bytes a scraper asks for — BOE/BOA PDFs,
REE CSV, etc. (The `PDF` in the class names is historical; it fetches any
content type.)

Tenacity handles transient 5xx / network errors with exponential backoff.
The semaphore + per-fetch sleep keep us inside each source's politeness rate
limit (configured per source: e.g. ~2s between BOE PDF fetches).
"""

from __future__ import annotations

import asyncio

import httpx
import structlog
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

log = structlog.get_logger()


@retry(
    retry=retry_if_exception_type((httpx.HTTPStatusError, httpx.TransportError)),
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=1, min=1, max=30),
    reraise=True,
)
async def get_with_retry(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    """GET `url` with retry on transient transport errors and 5xx.

    Retries `httpx.TransportError` (connection drops, RemoteProtocolError,
    timeouts) and 5xx — these are the transient failures that otherwise abort a
    long backfill. 4xx (incl. 404) are NOT retried: they're returned as-is so the
    caller can interpret them (e.g. BOE 404 → EmptyDay). Used by the sumario
    fetchers; the per-item PDFFetcher has its own equivalent retry.
    """
    resp = await client.get(url, headers=headers or {})
    if resp.status_code >= 500:
        resp.raise_for_status()  # raise → retried
    return resp


class PDFFetchError(Exception):
    """A file fetch failed after all retries. (Name is historical — any content type.)"""


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
        """Fetch a file as bytes. Returns the response body on success.

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
