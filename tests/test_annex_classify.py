"""Project-type/MW classifier + annex-fetch gate tests.

Calibrated on real BOE-B announcements (e.g. BOE-B-2026-21237: solar+wind,
50 MW → fetched via its wind component). The gate: fetch only storage/wind/
data-center ≥ MIN_MW; solar-only and below-threshold are skipped.
"""

from __future__ import annotations

from annex_ingest.classify import Classification, classify, should_fetch_annexes


def test_type_keywords() -> None:
    assert "wind" in classify("Parque eólico con aerogeneradores").types
    assert "solar" in classify("instalación fotovoltaica Teso Santo").types
    assert "storage" in classify("sistema de almacenamiento con baterías (BESS)").types
    assert "datacenter" in classify("proyecto de centro de datos / data center").types
    assert classify("anuncio de licitación de obras").types == ()


def test_storage_not_triggered_by_hibridacion() -> None:
    # Calibration fix: 'hibridación' (solar+wind) must NOT be read as storage.
    c = classify("planta fotovoltaica en hibridación con el parque eólico existente")
    assert "storage" not in c.types
    assert set(c.types) == {"solar", "wind"}


def test_mw_extraction() -> None:
    assert classify("potencia de 50 MW").max_mw == 50.0
    assert classify("36,5 MWp de pico").max_mw == 36.5
    assert classify("1.234,5 MW totales").max_mw == 1234.5
    assert classify("varias: 10 MW y 49,9 MW").max_mw == 49.9
    assert classify("sin potencia indicada").max_mw is None


def test_gate_in_scope_fetches() -> None:
    fetch, reason = should_fetch_annexes(Classification(types=("wind",), max_mw=50.0))
    assert fetch and "in_scope" in reason
    fetch, _ = should_fetch_annexes(Classification(types=("storage",), max_mw=25.0))
    assert fetch
    fetch, _ = should_fetch_annexes(Classification(types=("datacenter",), max_mw=100.0))
    assert fetch


def test_gate_hybrid_in_scope_wins() -> None:
    # solar + wind 50 MW → fetched because wind is in scope.
    fetch, reason = should_fetch_annexes(Classification(types=("solar", "wind"), max_mw=50.0))
    assert fetch and "wind" in reason


def test_gate_solar_only_skipped() -> None:
    fetch, reason = should_fetch_annexes(Classification(types=("solar",), max_mw=36.0))
    assert not fetch and reason == "solar_only"


def test_gate_below_threshold_skipped() -> None:
    fetch, reason = should_fetch_annexes(Classification(types=("wind",), max_mw=10.0))
    assert not fetch and reason.startswith("below_min_mw")


def test_gate_in_scope_unknown_mw_fetches() -> None:
    # Don't miss a big project just because the MW wasn't stated; flag it.
    fetch, reason = should_fetch_annexes(Classification(types=("storage",), max_mw=None))
    assert fetch and "mw_unknown" in reason


def test_gate_out_of_scope_skipped() -> None:
    fetch, reason = should_fetch_annexes(Classification(types=(), max_mw=None))
    assert not fetch and reason == "unclassified"
