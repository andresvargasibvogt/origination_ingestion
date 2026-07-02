"""Annex path-helper tests."""

from __future__ import annotations

from datetime import date

from annex_ingest.paths import annex_file_path, annex_state_path, safe_name


def test_safe_name_is_ascii_and_uuid_prefixed() -> None:
    name = safe_name("252efeb7-c5b0-46e6-b26d-b5a6739c661e", "Anexo 2_Copia solicitud EA-modificación.pdf")
    assert name.startswith("252efeb7-c5b0-46e6-b26d-b5a6739c661e__")
    assert name.isascii()                       # diacritics folded (ó -> o)
    assert " " not in name and "/" not in name  # path-safe
    assert name.endswith(".pdf") or "pdf" in name


def test_safe_name_collision_free_for_same_name() -> None:
    a = safe_name("11111111-1111-1111-1111-111111111111", "Plano.pdf")
    b = safe_name("22222222-2222-2222-2222-222222222222", "Plano.pdf")
    assert a != b  # different file uuids → different path segments


def test_annex_file_path_layout() -> None:
    p = annex_file_path("BOE-B-2026-21237", "15785610-da5b-4869-b4cd-d9fd1c837dc6",
                        "252efeb7-c5b0-46e6-b26d-b5a6739c661e", "Anexo 2.pdf")
    assert p.startswith("bronze/boe/annexes/BOE-B-2026-21237/15785610-da5b-4869-b4cd-d9fd1c837dc6/")


def test_annex_state_path_partitioned_by_day() -> None:
    p = annex_state_path(date(2026, 6, 22))
    assert p == "bronze/boe/annexes/_state/year=2026/month=06/day=22/_linked_documents.jsonl"
