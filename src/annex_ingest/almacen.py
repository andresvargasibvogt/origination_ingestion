"""Client for the almacen.redsara.es public file-exchange API.

Confirmed contract (anonymous, no auth; HTTP range supported):

  list:     GET /api/v1/sending/public/<uuid>
              -> {"status":"success","data":{"id":int,"expirationDate":"...",
                  "files":[{"identifier","name","size","mime"}, ...]}}
  per-file: GET /api/v1/sending/public/<uuid>/attachment/<file.identifier>
              -> raw bytes

Listing is a tiny JSON GET (reuses origination_common.fetcher.get_with_retry).
Downloads are STREAMED to a local file in bounded-memory chunks (files reach
~880 MB) with the sha256 computed inline. v1 restarts a file on retry rather
than HTTP-range-resuming (simpler + correct); range-resume is a future tweak.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime
from pathlib import Path

import httpx
import structlog
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from origination_common.fetcher import get_with_retry

from .config import ALMACEN_ATTACHMENT_PATH, ALMACEN_LIST_PATH

log = structlog.get_logger()

_MAX_DOWNLOAD_ATTEMPTS = 3


class AlmacenError(Exception):
    """Malformed response we don't know how to recover from."""


class AlmacenNotFound(Exception):
    """The sending or file returned 404/410 (gone)."""


class AlmacenExpired(Exception):
    """The sending's expirationDate has passed (link dead)."""


class AlmacenNetworkError(Exception):
    """Transient transport/5xx failure after retries."""


# Pydantic models — the list endpoint is untrusted external JSON, so we validate
# it on the way in (consistent with manifest.py / Settings using Pydantic).
class AlmacenFile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    identifier: str
    name: str = ""
    size: int = 0
    mime: str = "application/octet-stream"


class _SendingData(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    sending_id: int = Field(default=0, alias="id")
    expiration_date: str | None = Field(default=None, alias="expirationDate")
    files: list[AlmacenFile] = Field(default_factory=list)


class _SendingEnvelope(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: str = ""
    data: _SendingData = Field(default_factory=_SendingData)


class SendingListing(BaseModel):
    model_config = ConfigDict(frozen=True)

    sending_uuid: str
    sending_id: int
    expiration_date: str | None
    files: tuple[AlmacenFile, ...]

    def is_expired(self, *, now: datetime | None = None) -> bool:
        if not self.expiration_date:
            return False
        now = now or datetime.now(UTC)
        try:
            exp = datetime.fromisoformat(self.expiration_date)
        except ValueError:
            return False
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=UTC)
        return exp < now


def parse_listing(uuid: str, body: bytes | str) -> SendingListing:
    """Validate + parse the list-endpoint JSON envelope into a SendingListing.

    Raises AlmacenError on a non-success status or a malformed body.
    """
    try:
        env = _SendingEnvelope.model_validate_json(body)
    except ValidationError as exc:
        raise AlmacenError(f"almacen listing for {uuid} is malformed: {exc}") from exc
    if env.status != "success":
        raise AlmacenError(f"almacen listing for {uuid} not successful: status={env.status!r}")
    data = env.data
    files = tuple(f for f in data.files if f.identifier)
    return SendingListing(
        sending_uuid=uuid,
        sending_id=data.sending_id,
        expiration_date=data.expiration_date,
        files=files,
    )


class AlmacenClient:
    """Lists and downloads public sendings. Bounded concurrency + throttle."""

    def __init__(self, client: httpx.AsyncClient, *, concurrency: int, throttle_secs: float, chunk_bytes: int) -> None:
        self._client = client
        self._semaphore = asyncio.Semaphore(concurrency)
        self._throttle_secs = throttle_secs
        self._chunk_bytes = chunk_bytes

    async def list_sending(self, uuid: str) -> SendingListing:
        url = ALMACEN_LIST_PATH.format(uuid=uuid)
        try:
            resp = await get_with_retry(self._client, url)
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            raise AlmacenNetworkError(f"listing {uuid} failed: {exc}") from exc
        if resp.status_code in (404, 410):
            raise AlmacenNotFound(f"sending {uuid} not found (HTTP {resp.status_code})")
        resp.raise_for_status()
        return parse_listing(uuid, resp.content)

    async def download_to_file(self, uuid: str, file_id: str, dest: Path) -> tuple[str, int]:
        """Stream one file to `dest`, returning (sha256_hex, bytes_written).

        Memory-bounded (one chunk at a time). Retries transient transport/5xx
        errors, restarting the file each attempt. Raises AlmacenNotFound on
        404/410 (no retry) and AlmacenNetworkError after exhausting retries.
        """
        url = ALMACEN_ATTACHMENT_PATH.format(uuid=uuid, file_id=file_id)
        last_exc: Exception | None = None
        for attempt in range(1, _MAX_DOWNLOAD_ATTEMPTS + 1):
            hasher = hashlib.sha256()
            total = 0
            try:
                async with self._semaphore:
                    async with self._client.stream("GET", url) as resp:
                        if resp.status_code in (404, 410):
                            raise AlmacenNotFound(f"file {file_id} of {uuid} gone (HTTP {resp.status_code})")
                        resp.raise_for_status()
                        with dest.open("wb") as fh:
                            async for chunk in resp.aiter_bytes(self._chunk_bytes):
                                fh.write(chunk)
                                hasher.update(chunk)
                                total += len(chunk)
                    await asyncio.sleep(self._throttle_secs)
                return hasher.hexdigest(), total
            except AlmacenNotFound:
                raise
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                last_exc = exc
                log.warning("annex_download_retry", uuid=uuid, file_id=file_id, attempt=attempt, error=str(exc))
                await asyncio.sleep(min(2 ** attempt, 30))
        raise AlmacenNetworkError(f"download {file_id} of {uuid} failed after {_MAX_DOWNLOAD_ATTEMPTS}: {last_exc}")
