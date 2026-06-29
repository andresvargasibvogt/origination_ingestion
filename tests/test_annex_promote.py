"""Annex-promoter tests — verdict routing + state transitions, with fakes."""

from __future__ import annotations

from annex_ingest import promoter as ap
from annex_ingest.paths import annex_state_path
from annex_ingest.state import LinkedDocument, write_state
from origination_common.promoter import SCAN_RESULT_TAG, SCAN_VERDICT_CLEAN, SCAN_VERDICT_MALICIOUS

PUB_DAY = "2026-06-22"
STATE_PATH = annex_state_path(__import__("datetime").date(2026, 6, 22))
BLOB_PATH = "bronze/boe/annexes/BOE-B-2026-21237/u1/f1__Anexo.pdf"


def _meta() -> dict:
    return {
        "announcement_external_id": "BOE-B-2026-21237",
        "sending_uuid": "u1",
        "file_identifier": "f1",
        "file_name": "Anexo.pdf",
        "published_at": PUB_DAY,
        "url": "https://almacen.redsara.es/sending/public/u1",
    }


class FakeBlobClient:
    def __init__(self, name, verdict, deleted, copied):
        self.name = name
        self.url = f"https://stg/untrusted/{name}"
        self._verdict = verdict
        self._deleted = deleted
        self._copied = copied

    def get_blob_tags(self):
        return {SCAN_RESULT_TAG: self._verdict} if self._verdict is not None else {}

    def get_blob_properties(self):
        return type("P", (), {"metadata": _meta()})()

    def delete_blob(self):
        self._deleted.add(self.name)

    def start_copy_from_url(self, url):
        self._copied.add(self.name)


class FakeContainer:
    def __init__(self, names, verdict, deleted, copied):
        self._names, self._verdict, self._deleted, self._copied = names, verdict, deleted, copied

    def list_blobs(self, include=None):
        return [type("B", (), {"name": n})() for n in self._names]

    def get_blob_client(self, blob=None, **_):
        path = blob if blob is not None else _.get("path")
        return FakeBlobClient(path, self._verdict, self._deleted, self._copied)


class FakeBlobService:
    def __init__(self, names, verdict, deleted, copied):
        self._names, self._verdict, self._deleted, self._copied = names, verdict, deleted, copied

    def get_container_client(self, name):
        names = self._names if name == "untrusted" else []
        return FakeContainer(names, self._verdict, self._deleted, self._copied)


class FakeOneLake:
    def __init__(self, seed: dict[str, str]):
        self.store = dict(seed)
        self.streamed: list[str] = []

    def read_text(self, path):
        return self.store.get(path)

    def write_text(self, path, text, metadata=None):
        self.store[path] = text

    def stream_from_blob(self, path, blob_client, chunk_bytes=0):
        self.streamed.append(path)
        return 0


class FakeSettings:
    stg_account_name = "stg"
    stg_container_untrusted = "untrusted"
    stg_container_quarantine = "quarantine"
    fabric_workspace_name = "ws"
    fabric_lakehouse_name = "lh"
    azure_client_id = None


def _seed_state(status="fetched") -> dict[str, str]:
    io = type("IO", (), {"store": {}, "write_text": lambda self, p, t, metadata=None: self.store.__setitem__(p, t)})()
    rec = LinkedDocument(
        announcement_external_id="BOE-B-2026-21237", url="https://almacen.redsara.es/sending/public/u1",
        host="almacen", sending_uuid="u1", file_identifier="f1", file_name="Anexo.pdf",
        status=status, discovered_at="2026-06-22T07:30:00Z", file_path=BLOB_PATH,
    )
    write_state(io, STATE_PATH, {("BOE-B-2026-21237", "u1", "f1"): rec})
    return io.store


def _run(monkeypatch, verdict, seed):
    deleted: set[str] = set()
    copied: set[str] = set()
    onelake = FakeOneLake(seed)
    monkeypatch.setattr(ap, "load_common_settings", lambda: FakeSettings())
    monkeypatch.setattr(ap, "select_credential", lambda *_: object())
    monkeypatch.setattr(ap, "BlobServiceClient", lambda account_url, credential: FakeBlobService([BLOB_PATH], verdict, deleted, copied))
    monkeypatch.setattr(ap, "OneLakeWriter", lambda **_: onelake)
    rc = ap.main()
    return rc, deleted, copied, onelake


def _status_in(store) -> str:
    text = store[STATE_PATH]
    return LinkedDocument.model_validate_json(text.strip().splitlines()[0]).status


def test_clean_blob_promoted(monkeypatch) -> None:
    rc, deleted, copied, onelake = _run(monkeypatch, SCAN_VERDICT_CLEAN, _seed_state())
    assert rc == 0
    assert onelake.streamed == [BLOB_PATH]      # streamed to OneLake
    assert BLOB_PATH in deleted                 # then deleted from staging
    assert _status_in(onelake.store) == "promoted"


def test_malicious_blob_quarantined(monkeypatch) -> None:
    rc, deleted, copied, onelake = _run(monkeypatch, SCAN_VERDICT_MALICIOUS, _seed_state())
    assert onelake.streamed == []               # never promoted
    assert BLOB_PATH in copied and BLOB_PATH in deleted
    assert _status_in(onelake.store) == "quarantined"


def test_pending_blob_skipped(monkeypatch) -> None:
    rc, deleted, copied, onelake = _run(monkeypatch, None, _seed_state())
    assert onelake.streamed == [] and not deleted  # left for a later run


def test_unknown_verdict_scan_failed(monkeypatch) -> None:
    rc, deleted, copied, onelake = _run(monkeypatch, "Scan failed - timeout", _seed_state())
    assert onelake.streamed == [] and not deleted  # unverified ≠ clean → left in staging
    assert _status_in(onelake.store) == "scan_failed"
