"""almacen.redsara.es client tests — JSON parsing, expiry, and streamed hashing."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from annex_ingest.almacen import (
    AlmacenClient,
    AlmacenError,
    AlmacenNotFound,
    SendingListing,
    parse_listing,
)
from annex_ingest.config import ALMACEN_ATTACHMENT_PATH, ALMACEN_BASE_URL, ALMACEN_LIST_PATH
from origination_common.manifest import sha256_hex

# Real envelope captured from the spike (one PDF).
_LISTING = (
    '{"status":"success","data":{"id":150362,"subject":"Teso Santo",'
    '"expirationDate":"2026-09-16T23:59:59+02:00",'
    '"files":[{"identifier":"252efeb7-c5b0-46e6-b26d-b5a6739c661e",'
    '"name":"Anexo 2.pdf","size":1185837,"mime":"application/pdf"}]}}'
)


def test_parse_listing_ok() -> None:
    listing = parse_listing("15785610-da5b-4869-b4cd-d9fd1c837dc6", _LISTING)
    assert listing.sending_id == 150362
    assert listing.expiration_date == "2026-09-16T23:59:59+02:00"
    assert len(listing.files) == 1
    f = listing.files[0]
    assert f.identifier == "252efeb7-c5b0-46e6-b26d-b5a6739c661e"
    assert f.mime == "application/pdf"
    assert f.size == 1185837


def test_parse_listing_rejects_non_success() -> None:
    with pytest.raises(AlmacenError):
        parse_listing("x", '{"status":"error","message":"nope"}')


def test_parse_listing_rejects_garbage() -> None:
    with pytest.raises(AlmacenError):
        parse_listing("x", "<html>not json</html>")


def test_is_expired() -> None:
    past = SendingListing(sending_uuid="u", sending_id=1, expiration_date="2020-01-01T00:00:00+00:00", files=())
    future = SendingListing(sending_uuid="u", sending_id=1, expiration_date="2099-01-01T00:00:00+00:00", files=())
    none = SendingListing(sending_uuid="u", sending_id=1, expiration_date=None, files=())
    assert past.is_expired() is True
    assert future.is_expired() is False
    assert none.is_expired() is False


@respx.mock
async def test_download_to_file_streams_and_hashes(tmp_path: Path) -> None:
    uuid = "15785610-da5b-4869-b4cd-d9fd1c837dc6"
    fid = "252efeb7-c5b0-46e6-b26d-b5a6739c661e"
    payload = b"%PDF-1.6\n" + b"x" * 200_000  # bigger than one chunk
    respx.get(ALMACEN_BASE_URL + ALMACEN_ATTACHMENT_PATH.format(uuid=uuid, file_id=fid)).mock(
        return_value=httpx.Response(200, content=payload)
    )
    async with httpx.AsyncClient(base_url=ALMACEN_BASE_URL) as client:
        c = AlmacenClient(client, concurrency=1, throttle_secs=0.0, chunk_bytes=64 * 1024)
        dest = tmp_path / "out.bin"
        sha, nbytes = await c.download_to_file(uuid, fid, dest)
    assert nbytes == len(payload)
    assert sha == sha256_hex(payload)          # streamed hash == one-shot hash
    assert dest.read_bytes() == payload


@respx.mock
async def test_download_not_found_raises(tmp_path: Path) -> None:
    uuid, fid = "u", "f"
    respx.get(ALMACEN_BASE_URL + ALMACEN_ATTACHMENT_PATH.format(uuid=uuid, file_id=fid)).mock(
        return_value=httpx.Response(404)
    )
    async with httpx.AsyncClient(base_url=ALMACEN_BASE_URL) as client:
        c = AlmacenClient(client, concurrency=1, throttle_secs=0.0, chunk_bytes=1024)
        with pytest.raises(AlmacenNotFound):
            await c.download_to_file(uuid, fid, tmp_path / "x.bin")


@respx.mock
async def test_list_sending_parses(tmp_path: Path) -> None:
    uuid = "15785610-da5b-4869-b4cd-d9fd1c837dc6"
    respx.get(ALMACEN_BASE_URL + ALMACEN_LIST_PATH.format(uuid=uuid)).mock(
        return_value=httpx.Response(200, content=_LISTING)
    )
    async with httpx.AsyncClient(base_url=ALMACEN_BASE_URL) as client:
        c = AlmacenClient(client, concurrency=1, throttle_secs=0.0, chunk_bytes=1024)
        listing = await c.list_sending(uuid)
    assert listing.files[0].size == 1185837
