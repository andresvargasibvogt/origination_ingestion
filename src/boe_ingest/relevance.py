"""Relevance filter — section + departamento, matching the human's manual selection.

The human filters boe.es by SECCIÓN + DEPARTAMENTO on the daily index page.
No title-keyword matching. This module encodes those rules.

A rule is `(section, departamento criteria)`. An item passes if ANY rule
matches its `(section, departamento)`. Rules are loaded from YAML so they
can be edited without rebuilding the container image.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class Rule:
    section: str
    departamento_codigos: frozenset[str]
    issuer_name_patterns: tuple[re.Pattern[str], ...]

    def matches(self, section: str, departamento: dict[str, Any]) -> bool:
        if section != self.section:
            return False
        codigo = str(departamento.get("codigo", "")).strip()
        if codigo and codigo in self.departamento_codigos:
            return True
        nombre = str(departamento.get("nombre", ""))
        return any(p.search(nombre) for p in self.issuer_name_patterns)


@dataclass(frozen=True)
class RelevanceConfig:
    rules: tuple[Rule, ...]

    @classmethod
    def load(cls, path: Path) -> RelevanceConfig:
        raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
        raw_rules = raw.get("rules")
        if not raw_rules:
            raise ValueError(f"relevance config at {path} has no 'rules' list")

        rules: list[Rule] = []
        for i, r in enumerate(raw_rules):
            section = str(r.get("section", "")).strip()
            if not section:
                raise ValueError(f"rule #{i} missing 'section'")
            codigos = frozenset(str(c) for c in r.get("departamento_codigos", []))
            patterns = tuple(re.compile(p) for p in r.get("issuer_name_patterns", []))
            if not codigos and not patterns:
                raise ValueError(
                    f"rule #{i} (section={section}) has no departamento criteria; "
                    "at least one of departamento_codigos or issuer_name_patterns required"
                )
            rules.append(
                Rule(
                    section=section,
                    departamento_codigos=codigos,
                    issuer_name_patterns=patterns,
                )
            )
        return cls(rules=tuple(rules))


def passes_filter(
    item: dict[str, Any],
    departamento: dict[str, Any],
    section: str,
    config: RelevanceConfig,
) -> bool:
    """Return True iff `(section, departamento)` matches any rule."""
    _ = item  # signature kept stable; future rules may inspect item fields
    return any(rule.matches(section, departamento) for rule in config.rules)
