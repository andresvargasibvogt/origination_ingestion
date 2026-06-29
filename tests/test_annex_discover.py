"""Annex link-discovery tests — the regex is the load-bearing piece.

Must capture almacen.redsara.es sending UUIDs, EXCLUDE the rec.redsara.es
registry (a submission portal, not a document), and ignore the legacy ssweb
host in v1. Modeled on the real BOE-B-2026-21237 XML body.
"""

from __future__ import annotations

from datetime import date

from annex_ingest.discover import (
    count_legacy_links,
    extract_publication_date,
    extract_sending_uuids,
)

_XML = """<?xml version="1.0"?>
<documento>
  <metadatos><fecha_publicacion>20260622</fecha_publicacion></metadatos>
  <texto>
    Presentarse en el registro electrónico https://rec.redsara.es para alegaciones.
    Documentación: https://almacen.redsara.es/sending/public/15785610-da5b-4869-b4cd-d9fd1c837dc6
    y https://almacen.redsara.es/sending/public/dad478ad-6041-4772-9840-82240c22fdbd
    y https://almacen.redsara.es/sending/public/37478302-9a11-4aba-809c-0013ee9f01ac
  </texto>
</documento>"""


def test_extracts_almacen_uuids_excludes_rec() -> None:
    uuids = extract_sending_uuids(_XML)
    assert uuids == [
        "15785610-da5b-4869-b4cd-d9fd1c837dc6",
        "dad478ad-6041-4772-9840-82240c22fdbd",
        "37478302-9a11-4aba-809c-0013ee9f01ac",
    ]
    # rec.redsara.es is a registry host, not almacen → never captured.
    assert all("rec.redsara.es" not in u for u in uuids)


def test_dedupes_repeated_uuid_order_preserved() -> None:
    dup = _XML + "\nhttps://almacen.redsara.es/sending/public/15785610-da5b-4869-b4cd-d9fd1c837dc6\n"
    uuids = extract_sending_uuids(dup)
    assert uuids.count("15785610-da5b-4869-b4cd-d9fd1c837dc6") == 1


def test_zero_links() -> None:
    assert extract_sending_uuids("<texto>nada que ver con redsara</texto>") == []


def test_legacy_ssweb_counted_not_captured() -> None:
    body = "https://ssweb.seap.minhap.es/almacen/descarga/envio/" + "a" * 40
    assert extract_sending_uuids(body) == []      # deferred host, not an almacen uuid
    assert count_legacy_links(body) == 1          # but counted for visibility


def test_publication_date() -> None:
    assert extract_publication_date(_XML) == date(2026, 6, 22)
    assert extract_publication_date("<documento/>") is None
