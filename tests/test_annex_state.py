"""JSONL linked_document state-manifest tests."""

from __future__ import annotations

from annex_ingest.state import LinkedDocument, key_of, read_state, upsert, write_state


class FakeIO:
    """In-memory read_text/write_text store (stands in for OneLakeWriter/LocalWriter)."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def read_text(self, path: str) -> str | None:
        return self.store.get(path)

    def write_text(self, path: str, text: str, metadata=None) -> None:
        self.store[path] = text


def _doc(status: str, **kw) -> LinkedDocument:
    base = dict(
        announcement_external_id="BOE-B-2026-21237",
        url="https://almacen.redsara.es/sending/public/u1",
        host="almacen", sending_uuid="u1", file_identifier="f1",
        file_name="Anexo.pdf", status=status, discovered_at="2026-06-22T07:30:00Z",
    )
    base.update(kw)
    return LinkedDocument(**base)


def test_round_trip() -> None:
    io = FakeIO()
    path = "bronze/boe/annexes/_state/year=2026/month=06/day=22/_linked_documents.jsonl"
    recs = {}
    upsert(recs, _doc("fetched", content_hash="a" * 64, bytes=10))
    write_state(io, path, recs)
    back = read_state(io, path)
    assert len(back) == 1
    rec = next(iter(back.values()))
    assert rec.status == "fetched"
    assert rec.content_hash == "a" * 64


def test_missing_file_returns_empty() -> None:
    assert read_state(FakeIO(), "nope.jsonl") == {}


def test_upsert_last_write_wins() -> None:
    recs = {}
    upsert(recs, _doc("pending"))
    upsert(recs, _doc("fetched", content_hash="b" * 64))
    assert len(recs) == 1                       # same (announcement, sending, file) key
    assert next(iter(recs.values())).status == "fetched"


def test_key_distinguishes_files() -> None:
    a = _doc("fetched", file_identifier="f1")
    b = _doc("fetched", file_identifier="f2")
    assert key_of(a) != key_of(b)
    recs = {}
    upsert(recs, a)
    upsert(recs, b)
    assert len(recs) == 2
