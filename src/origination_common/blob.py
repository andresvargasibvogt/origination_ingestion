"""Azure Blob Storage writer for the staging account (ADR-008).

Every scraper writes its fetched files here (BOE/BOA PDFs, REE CSV). Per-blob
metadata captures the per-item context (identifier, section,
departamento_codigo, etc.) so the promoter can build the OneLake manifest
later without re-parsing anything.

The path layout INSIDE the container mirrors the OneLake bronze layout, e.g.

    {container}/bronze/boe/raw/year=YYYY/month=MM/day=DD/{identifier}.pdf   (daily source)
    {container}/bronze/ree/raw/year=YYYY/month=MM/{filename}.csv            (monthly source)

That way the promoter's "copy from staging to OneLake" is a path-preserving
copy — no rewriting needed.
"""

from __future__ import annotations

import json
import os
import unicodedata
from typing import Any

import structlog
from azure.storage.blob import BlobServiceClient, ContentSettings

from .onelake import select_credential

log = structlog.get_logger()


class BlobWriter:
    """Writes bytes/text to a single container in an Azure Storage Account.

    Path inside the container is the same lakehouse-relative path the
    OneLakeWriter uses. Per-blob metadata is attached so the promoter can
    extract it without re-fetching anything.
    """

    def __init__(
        self,
        account_name: str,
        container: str,
        azure_client_id: str | None = None,
    ) -> None:
        self._account_name = account_name
        self._container = container
        account_url = f"https://{account_name}.blob.core.windows.net"
        self._service = BlobServiceClient(
            account_url=account_url,
            credential=select_credential(azure_client_id),
        )

    def write_bytes(
        self,
        path: str,
        data: bytes,
        metadata: dict[str, str] | None = None,
        content_type: str = "application/octet-stream",
    ) -> None:
        """Upload `data` to {container}/{path}.

        `metadata` becomes Azure blob metadata (key-value strings on the
        blob). Defender for Storage scans the bytes and writes the scan
        verdict as a blob INDEX TAG (a different mechanism); we use metadata
        for our own per-item context.
        """
        client = self._service.get_blob_client(container=self._container, blob=path)
        client.upload_blob(
            data,
            overwrite=True,
            metadata=_stringify_metadata(metadata or {}),
            content_settings=ContentSettings(content_type=content_type),
        )
        log.info(
            "blob_write_ok",
            account=self._account_name,
            container=self._container,
            path=path,
            bytes=len(data),
        )

    def write_text(
        self,
        path: str,
        text: str,
        metadata: dict[str, str] | None = None,
        content_type: str = "text/plain; charset=utf-8",
    ) -> None:
        self.write_bytes(path, text.encode("utf-8"), metadata=metadata, content_type=content_type)

    def write_json(
        self,
        path: str,
        obj: Any,
        metadata: dict[str, str] | None = None,
    ) -> None:
        self.write_text(
            path,
            json.dumps(obj, indent=2, ensure_ascii=False),
            metadata=metadata,
            content_type="application/json",
        )

    def put_file(
        self,
        path: str,
        local_fspath: str,
        metadata: dict[str, str] | None = None,
        content_type: str = "application/octet-stream",
    ) -> None:
        """Stream a local file to {container}/{path} without buffering it in memory.

        Used for large annexes: the Azure SDK chunks the upload from the file
        handle, so a multi-hundred-MB file never lands in RAM whole.
        """
        client = self._service.get_blob_client(container=self._container, blob=path)
        with open(local_fspath, "rb") as fh:
            client.upload_blob(
                fh,
                overwrite=True,
                metadata=_stringify_metadata(metadata or {}),
                content_settings=ContentSettings(content_type=content_type),
                max_concurrency=2,
            )
        log.info(
            "blob_put_file_ok",
            account=self._account_name,
            container=self._container,
            path=path,
            bytes=os.path.getsize(local_fspath),
        )


def _stringify_metadata(metadata: dict[str, Any]) -> dict[str, str]:
    """Azure blob metadata only accepts ASCII string values.

    Spanish names like "Ministerio para la Transición Ecológica" contain
    non-ASCII characters that Azure rejects with InvalidMetadata. We
    ASCII-fold values here ("Transición" -> "Transicion") — the canonical
    Unicode original is preserved in the OneLake manifest (written by the
    promoter as UTF-8 JSON); only the per-blob metadata label is folded.
    """
    result: dict[str, str] = {}
    for k, v in metadata.items():
        if v is None:
            continue
        # Keys must be valid C# identifiers — ASCII alphanum + underscore.
        # Our keys (identifier, section, departamento_codigo, etc.) are fine.
        result[k] = _ascii_fold(str(v))
    return result


def _ascii_fold(s: str) -> str:
    """Strip diacritics so the result is plain ASCII."""
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
