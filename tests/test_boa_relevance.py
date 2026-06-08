"""BOA relevance filter calibration test.

Mirrors `tests/test_relevance.py` for the BOA filter. The fixture set in
`fixtures/boa_calibration_set.yaml` is curated against real BOA data
(2026-06-01..2026-06-08) and acts as the regression gate when the filter
rules in `src/boa_ingest/relevance.yaml` change.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from boa_ingest.config import load_settings
from boa_ingest.relevance import RelevanceConfig, passes_filter

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "boa_calibration_set.yaml"


@pytest.fixture(scope="module")
def relevance_config() -> RelevanceConfig:
    settings = load_settings()
    return RelevanceConfig.load(settings.relevance_config_path)


@pytest.fixture(scope="module")
def calibration_set() -> list[dict]:
    raw = yaml.safe_load(FIXTURE_PATH.read_text(encoding="utf-8"))
    entries: list[dict] = []
    for e in raw["positives"]:
        entries.append({**e, "expected": True})
    for e in raw["negatives"]:
        entries.append({**e, "expected": False})
    return entries


def test_calibration_no_misclassifications(
    calibration_set: list[dict],
    relevance_config: RelevanceConfig,
) -> None:
    """Every fixture entry's predicted classification must match `expected`."""
    misclassifications: list[str] = []
    for entry in calibration_set:
        predicted = passes_filter(
            section=entry["section"],
            subsection=entry.get("subsection"),
            departamento=entry["departamento"],
            config=relevance_config,
        )
        if predicted != entry["expected"]:
            misclassifications.append(
                f"  section={entry['section']!r} subsection={entry.get('subsection')!r} "
                f"departamento={entry['departamento']!r} "
                f"expected={entry['expected']} got={predicted} "
                f"({entry.get('note', '')})"
            )
    assert not misclassifications, (
        "BOA relevance misclassifications:\n" + "\n".join(misclassifications)
    )


def test_relevance_config_loads_with_expected_shape(
    relevance_config: RelevanceConfig,
) -> None:
    """The shipped relevance.yaml must have at least one rule."""
    assert relevance_config.rules, "boa_ingest/relevance.yaml has no rules"


def test_relevance_config_rejects_empty_rules(tmp_path: Path) -> None:
    bad = tmp_path / "empty.yaml"
    bad.write_text("rules: []\n", encoding="utf-8")
    with pytest.raises(ValueError, match="no 'rules'"):
        RelevanceConfig.load(bad)


def test_relevance_config_rejects_rule_without_departamento(tmp_path: Path) -> None:
    bad = tmp_path / "incomplete.yaml"
    bad.write_text(
        "rules:\n  - section: V\n    subsection: b\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="departamento_name"):
        RelevanceConfig.load(bad)
