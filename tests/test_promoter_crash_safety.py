"""Crash-safety regression tests for the shared promoter.

The promoter's load-bearing invariant: a staged blob is deleted ONLY after its
partition manifest is durably written to OneLake. An earlier version deleted
blobs in the blob pass and wrote manifests in a later pass, so a crash between
the passes drained staging but lost the manifests (PDFs in OneLake, no
manifest) — exactly how BOE 2020/2021 lost their manifests. These tests fake
the Azure clients and assert the invariant holds, including under a simulated
crash mid-manifest-write.
"""

from __future__ import annotations

import json

import pytest
from azure.core.exceptions import ResourceNotFoundError

from origination_common import promoter

CLEAN_TAG = {promoter.SCAN_RESULT_TAG: promoter.SCAN_VERDICT_CLEAN}


def _meta(identifier: str) -> dict[str, str]:
    return {
        "identifier": identifier,
        "section": "3",
        "departamento_codigo": "9575",
        "departamento": "Test",
        "published_at": "2021-03-08",
        "sha256": "0" * 64,
        "size_bytes": "10",
        "url_pdf": "https://example/x.pdf",
    }


class FakeBlob:
    def __init__(self, name: str):
        self.name = name


class FakeBlobClient:
    def __init__(self, path: str, deleted: set[str]):
        self.path = path
        self._deleted = deleted
        self.url = f"https://stg/untrusted/{path}"

    def get_blob_tags(self):
        return CLEAN_TAG

    def get_blob_properties(self):
        ident = self.path.rsplit("/", 1)[-1].removesuffix(".pdf")
        return type("Props", (), {"metadata": _meta(ident)})()

    def download_blob(self):
        return type("DL", (), {"readall": staticmethod(lambda: b"%PDF-1.4 fake")})()

    def delete_blob(self):
        self._deleted.add(self.path)


class FakeContainer:
    def __init__(self, names: list[str], deleted: set[str]):
        self._names = names
        self._deleted = deleted

    def list_blobs(self, include=None):
        return [FakeBlob(n) for n in self._names]

    def get_blob_client(self, path: str):
        return FakeBlobClient(path, self._deleted)


class FakeBlobService:
    def __init__(self, names: list[str], deleted: set[str]):
        self._names = names
        self._deleted = deleted

    def get_container_client(self, name: str):
        # untrusted has the blobs; quarantine is empty here.
        return FakeContainer(self._names if name == "untrusted" else [], self._deleted)


class FakeFileClient:
    def download_file(self):
        raise ResourceNotFoundError("no existing manifest")  # force fresh manifests


class FakeService:
    def get_file_client(self, **_):
        return FakeFileClient()


class FakeOneLake:
    """Records writes; can be told to raise on a specific manifest path."""

    def __init__(self, fail_manifest_substr: str | None = None):
        self.written: dict[str, bytes] = {}
        self.manifests: dict[str, str] = {}
        self._fail = fail_manifest_substr
        self._lakehouse_files_prefix = "lh.Lakehouse/Files"
        self._workspace = "ws"
        self._service = FakeService()

    def write_bytes(self, path: str, data: bytes, metadata=None):
        self.written[path] = data

    def write_text(self, path: str, text: str, metadata=None):
        if self._fail and self._fail in path:
            raise RuntimeError(f"simulated crash writing manifest {path}")
        self.manifests[path] = text


class FakeSettings:
    stg_account_name = "stg"
    stg_container_untrusted = "untrusted"
    stg_container_quarantine = "quarantine"
    fabric_workspace_name = "ws"
    fabric_lakehouse_name = "lh"
    azure_client_id = None


def _patch(monkeypatch, names, deleted, onelake):
    monkeypatch.setattr(promoter, "load_common_settings", lambda: FakeSettings())
    monkeypatch.setattr(promoter, "select_credential", lambda *_: object())
    monkeypatch.setattr(
        promoter, "BlobServiceClient", lambda account_url, credential: FakeBlobService(names, deleted)
    )
    monkeypatch.setattr(promoter, "OneLakeWriter", lambda **_: onelake)


# Two BOE partitions, two PDFs each.
NAMES = [
    "bronze/boe/raw/year=2021/month=03/day=08/BOE-A-2021-0001.pdf",
    "bronze/boe/raw/year=2021/month=03/day=08/BOE-A-2021-0002.pdf",
    "bronze/boe/raw/year=2021/month=03/day=09/BOE-A-2021-0003.pdf",
    "bronze/boe/raw/year=2021/month=03/day=09/BOE-A-2021-0004.pdf",
]


def test_happy_path_writes_manifests_then_deletes_all(monkeypatch):
    deleted: set[str] = set()
    onelake = FakeOneLake()
    _patch(monkeypatch, NAMES, deleted, onelake)

    rc = promoter.main()
    assert rc == 0

    # All four PDFs copied to OneLake.
    assert set(onelake.written) == set(NAMES)
    # One manifest per day, each cataloguing its two items.
    assert len(onelake.manifests) == 2
    for text in onelake.manifests.values():
        assert len(json.loads(text)["items"]) == 2
    # Every staged blob drained — only after its manifest was written.
    assert deleted == set(NAMES)


def test_crash_during_manifest_write_keeps_that_partitions_blobs(monkeypatch):
    """If a partition's manifest write fails, its blobs MUST remain in staging
    (so a re-run rebuilds the manifest) — never deleted ahead of the manifest."""
    deleted: set[str] = set()
    onelake = FakeOneLake(fail_manifest_substr="day=09")  # day=09 manifest write crashes
    _patch(monkeypatch, NAMES, deleted, onelake)

    with pytest.raises(RuntimeError, match="simulated crash"):
        promoter.main()

    day08 = {n for n in NAMES if "day=08" in n}
    day09 = {n for n in NAMES if "day=09" in n}
    # day=08 manifest landed → its blobs were drained.
    assert any("day=08" in p for p in onelake.manifests)
    assert day08 <= deleted
    # day=09 manifest crashed → its blobs are still staged for the next run.
    assert not (day09 & deleted)
