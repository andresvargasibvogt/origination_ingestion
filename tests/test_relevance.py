"""Relevance filter calibration test.

The filter is section + departamento — matching the human's manual
selection on https://boe.es/boe/dias/{Y}/{M}/{D}/index.php.

This test is the regression gate: every fixture entry must be classified
correctly. When `relevance.yaml` changes, any drift surfaces here.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from boe_ingest.config import load_settings
from boe_ingest.relevance import RelevanceConfig, passes_filter

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "calibration_set.yaml"


@pytest.fixture(scope="module")
def relevance_config() -> RelevanceConfig:
    settings = load_settings()
    return RelevanceConfig.load(settings.relevance_config_path)


@pytest.fixture(scope="module")
def calibration_set() -> list[dict]:
    raw = yaml.safe_load(FIXTURE_PATH.read_text(encoding="utf-8"))
    return raw["positives"] + raw["negatives"]


def test_calibration_no_misclassifications(
    calibration_set: list[dict], relevance_config: RelevanceConfig
) -> None:
    """Every fixture entry's predicted classification must match `expected`.

    This is the gate — any miss here means the filter changed in a way
    that drops real items or accepts irrelevant ones.
    """
    misclassifications: list[str] = []
    for entry in calibration_set:
        item: dict = {}  # filter is structural; title not used
        predicted = passes_filter(
            item,
            entry["departamento"],
            entry["section"],
            relevance_config,
        )
        if predicted != entry["expected"]:
            misclassifications.append(
                f"{entry['identifier']} section={entry['section']} "
                f"departamento_codigo={entry['departamento'].get('codigo')!r} "
                f"departamento_nombre={entry['departamento'].get('nombre')!r} "
                f"expected={entry['expected']} predicted={predicted}"
            )

    assert not misclassifications, (
        f"Misclassifications: {len(misclassifications)}\n" + "\n".join(misclassifications)
    )


def test_relevance_config_loads_with_expected_shape(relevance_config: RelevanceConfig) -> None:
    """Sanity check that the YAML produces at least the MITECO Section 3 rule."""
    assert relevance_config.rules, "no rules loaded"
    miteco_section3_rule = any(
        r.section == "3" and "9575" in r.departamento_codigos
        for r in relevance_config.rules
    )
    assert miteco_section3_rule, "MITECO rule (section='3' + codigo='9575') missing"


def test_relevance_config_rejects_empty_rules(tmp_path: Path) -> None:
    """A misconfigured YAML must fail loudly, not silently match everything."""
    bad = tmp_path / "bad.yaml"
    bad.write_text("rules: []\n", encoding="utf-8")
    with pytest.raises(ValueError):
        RelevanceConfig.load(bad)


def test_relevance_config_rejects_rule_without_criteria(tmp_path: Path) -> None:
    """A rule must have at least one departamento criterion."""
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "rules:\n  - section: 'III'\n    departamento_codigos: []\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        RelevanceConfig.load(bad)
