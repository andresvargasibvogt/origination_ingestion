"""Relevance filter — section + subsection + departamento name.

BOA has subsections inside section V (V.a, V.b, V.c — Anuncios). This filter
encodes the user's structural criteria from the boa.aragon.es daily index:
match section, optionally subsection, and the departamento full name.

Rules are loaded from YAML so they can be edited without rebuilding the
container image.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class Rule:
    section: str
    subsection: str | None
    departamento_name: str

    def matches(
        self,
        section: str,
        subsection: str | None,
        departamento: str,
    ) -> bool:
        if _norm(section) != _norm(self.section):
            return False
        # If the rule specifies a subsection, it must match. If not, any subsection passes.
        if self.subsection is not None and _norm(subsection or "") != _norm(self.subsection):
            return False
        return _norm(departamento) == _norm(self.departamento_name)


def _norm(s: str) -> str:
    """Case-insensitive + whitespace-insensitive comparison.

    BOA reskins occasionally tweak whitespace inside headings; comparing on
    a normalised form keeps the filter resilient without overfitting.
    """
    return " ".join(s.split()).strip().lower()


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
            departamento = str(r.get("departamento_name", "")).strip()
            if not departamento:
                raise ValueError(
                    f"rule #{i} (section={section}) missing 'departamento_name'"
                )
            subsection_raw = r.get("subsection")
            subsection = str(subsection_raw).strip() if subsection_raw is not None else None
            rules.append(
                Rule(
                    section=section,
                    subsection=subsection,
                    departamento_name=departamento,
                )
            )
        return cls(rules=tuple(rules))


def passes_filter(
    section: str,
    subsection: str | None,
    departamento: str,
    config: RelevanceConfig,
) -> bool:
    """Return True iff `(section, subsection, departamento)` matches any rule."""
    return any(rule.matches(section, subsection, departamento) for rule in config.rules)
