"""Writers — OneLake (production) and local filesystem (dry-run / dev).

Both implement the same `Writer` Protocol so the orchestrator is identical
in either mode.

Credential selection follows Microsoft's recommended production pattern
(learn.microsoft.com → "Authenticate Azure-hosted Python apps using
managed identity"):

  - In Azure (IDENTITY_ENDPOINT env var set): use ManagedIdentityCredential
    explicitly, with the user-assigned MI client_id if provided.
  - Locally (no IDENTITY_ENDPOINT): use DefaultAzureCredential so dev
    tooling (az CLI / VS Code) picks up the developer's identity.

Why not DefaultAzureCredential everywhere? Microsoft recommends specific
credentials in production: faster (no chain walk), easier to debug
(no surprise fallback), more predictable (no env-var sensitivity).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Protocol

import structlog
from azure.core.credentials import TokenCredential
from azure.identity import DefaultAzureCredential, ManagedIdentityCredential
from azure.storage.filedatalake import DataLakeServiceClient

from .config import ONELAKE_ACCOUNT_URL

log = structlog.get_logger()


def select_credential(azure_client_id: str | None = None) -> TokenCredential:
    """Return the right credential for the current environment.

    `IDENTITY_ENDPOINT` is set by ACA, App Service, Functions, AKS workload
    identity, and other Azure compute platforms. Its presence is a reliable
    signal that we're running in a managed-identity-capable environment.
    """
    if os.getenv("IDENTITY_ENDPOINT"):
        log.info(
            "credential_selected",
            kind="ManagedIdentityCredential",
            client_id=bool(azure_client_id),
        )
        if azure_client_id:
            return ManagedIdentityCredential(client_id=azure_client_id)
        return ManagedIdentityCredential()
    log.info("credential_selected", kind="DefaultAzureCredential")
    return DefaultAzureCredential()


class Writer(Protocol):
    """Common interface for OneLake, blob, and local writers.

    `metadata` is optional per-blob context. Writers that support metadata
    (BlobWriter) attach it; others (LocalWriter, OneLakeWriter) may ignore
    it or write a sidecar file.
    """

    def write_bytes(self, lakehouse_path: str, data: bytes, metadata: dict[str, str] | None = None) -> None: ...

    def write_text(self, lakehouse_path: str, text: str, metadata: dict[str, str] | None = None) -> None: ...


class OneLakeWriter:
    """Writes bytes / text to a Fabric lakehouse via the OneLake ABFS API.

    `lakehouse_path` is RELATIVE to the lakehouse's `Files/` root, e.g.
    `bronze/boe/raw/year=2026/month=06/day=02/BOE-A-2026-XXXX.pdf`.
    """

    def __init__(
        self,
        workspace_name: str,
        lakehouse_name: str,
        azure_client_id: str | None = None,
    ) -> None:
        self._workspace = workspace_name
        self._lakehouse_files_prefix = f"{lakehouse_name}.Lakehouse/Files"
        self._service = DataLakeServiceClient(
            account_url=ONELAKE_ACCOUNT_URL,
            credential=select_credential(azure_client_id),
        )

    def write_bytes(self, lakehouse_path: str, data: bytes, metadata: dict[str, str] | None = None) -> None:
        # OneLakeWriter ignores `metadata` — OneLake's ADLS doesn't have a
        # blob-metadata equivalent we can hang per-item context on.
        del metadata
        full_path = f"{self._lakehouse_files_prefix}/{lakehouse_path.lstrip('/')}"
        file_client = self._service.get_file_client(
            file_system=self._workspace, file_path=full_path
        )
        file_client.upload_data(data, overwrite=True)
        log.info(
            "onelake_write_ok",
            workspace=self._workspace,
            path=full_path,
            bytes=len(data),
        )

    def write_text(self, lakehouse_path: str, text: str, metadata: dict[str, str] | None = None) -> None:
        self.write_bytes(lakehouse_path, text.encode("utf-8"), metadata=metadata)

    def write_json(self, lakehouse_path: str, obj: object) -> None:
        self.write_text(lakehouse_path, json.dumps(obj, indent=2, ensure_ascii=False))


class LocalWriter:
    """Writes to a local directory. Mirrors the lakehouse layout.

    Used by `--out-dir` for dry runs and local testing — no Azure creds
    required.
    """

    def __init__(self, out_dir: Path) -> None:
        self._out_dir = out_dir

    def write_bytes(self, lakehouse_path: str, data: bytes, metadata: dict[str, str] | None = None) -> None:
        target = self._out_dir / lakehouse_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        # For local debugging, drop metadata as a sidecar .meta.json next to the file.
        if metadata:
            sidecar = target.with_suffix(target.suffix + ".meta.json")
            sidecar.write_text(json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8")
        log.info("local_write_ok", path=str(target), bytes=len(data))

    def write_text(self, lakehouse_path: str, text: str, metadata: dict[str, str] | None = None) -> None:
        self.write_bytes(lakehouse_path, text.encode("utf-8"), metadata=metadata)

    def write_json(self, lakehouse_path: str, obj: object) -> None:
        self.write_text(lakehouse_path, json.dumps(obj, indent=2, ensure_ascii=False))
