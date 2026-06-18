"""Calibration gate for the BACKFILL relevance configs.

These tests load the historical-aware `relevance.backfill.yaml` for each source
(NOT the daily `relevance.yaml`) and assert zero misclassifications across the
calibration fixtures. As historical eras are added to the backfill configs
(after enumeration + confirmation), their clusters are added to the fixtures
here — so "the backfill filter is correct across every era" stays CI-checkable.

The daily-filter tests live in test_relevance.py / test_boa_relevance.py and are
deliberately untouched by the backfill work.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from boa_ingest import relevance as boa_relevance
from boe_ingest import relevance as boe_relevance

_FIX = Path(__file__).parent / "fixtures"
_BOE_BACKFILL_YAML = Path(boe_relevance.__file__).parent / "relevance.backfill.yaml"
_BOA_BACKFILL_YAML = Path(boa_relevance.__file__).parent / "relevance.backfill.yaml"


def _entries(fixture_name: str) -> list[dict]:
    raw = yaml.safe_load((_FIX / fixture_name).read_text(encoding="utf-8"))
    out: list[dict] = []
    for e in raw.get("positives", []):
        out.append({**e, "expected": True})
    for e in raw.get("negatives", []):
        out.append({**e, "expected": False})
    return out


def test_boe_backfill_filter_no_misclassifications() -> None:
    config = boe_relevance.RelevanceConfig.load(_BOE_BACKFILL_YAML)
    misses: list[str] = []
    for e in _entries("boe_backfill_calibration_set.yaml"):
        predicted = boe_relevance.passes_filter({}, e["departamento"], e["section"], config)
        if predicted != e["expected"]:
            misses.append(
                f"  section={e['section']!r} dep={e['departamento']!r} "
                f"expected={e['expected']} got={predicted}"
            )
    assert not misses, "BOE backfill misclassifications:\n" + "\n".join(misses)


def test_boa_backfill_filter_no_misclassifications() -> None:
    config = boa_relevance.RelevanceConfig.load(_BOA_BACKFILL_YAML)
    misses: list[str] = []
    for e in _entries("boa_backfill_calibration_set.yaml"):
        predicted = boa_relevance.passes_filter(
            section=e["section"],
            subsection=e.get("subsection"),
            departamento=e["departamento"],
            config=config,
        )
        if predicted != e["expected"]:
            misses.append(
                f"  section={e['section']!r} sub={e.get('subsection')!r} "
                f"dep={e['departamento']!r} expected={e['expected']} got={predicted}"
            )
    assert not misses, "BOA backfill misclassifications:\n" + "\n".join(misses)


@pytest.mark.parametrize("yaml_path", [_BOE_BACKFILL_YAML, _BOA_BACKFILL_YAML])
def test_backfill_config_is_superset_of_daily(yaml_path: Path) -> None:
    """The backfill config must exist and carry at least the current rules."""
    assert yaml_path.exists(), f"missing {yaml_path}"
    data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    assert data.get("rules"), f"{yaml_path} has no rules"
