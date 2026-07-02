"""Heuristic project classifier — the annex pre-download gate.

The BOE pipeline is unchanged; this only decides WHICH announcements' annexes to
download. We classify the announcement text (the XML the annex tier already
fetches during discovery) by project type + capacity (MW), then gate per the
agreed scope:

  - fetch annexes only if the project includes an IN-SCOPE tech
    (storage / wind / data center) AND capacity >= MIN_MW (default 20);
  - "in-scope wins" on hybrids (a solar+wind project is fetched via its wind);
  - solar-only projects are skipped here (their access date is a downstream /
    extraction concern, not the annex tier);
  - in-scope tech with NO stated MW → fetched and flagged (don't miss a big
    project just because the announcement omitted the number).

Heuristic keyword + MW-regex matching, calibrated on real BOE-B announcements
(e.g. BOE-B-2026-21237 → wind 50 MW → fetch). It is intentionally a cheap gate,
not authoritative extraction; the downstream LLM pass can refine later.
"""

from __future__ import annotations

import re

from pydantic import BaseModel

# Techs whose annexes we want; solar is recognized but not in-scope for fetching.
IN_SCOPE: frozenset[str] = frozenset({"storage", "wind", "datacenter"})
DEFAULT_MIN_MW: float = 20.0

_TYPE_PATTERNS: dict[str, re.Pattern[str]] = {
    # Battery/energy storage — NOT "hibridación" (that's solar+wind hybridization,
    # a calibration false-positive on the real BOE-B-2026-21237).
    "storage": re.compile(r"almacenamiento|bater[íi]a|\bBESS\b|sistema de almacenamiento", re.I),
    "wind": re.compile(r"e[óo]lic[oa]|aerogenerador|parque e[óo]lico", re.I),
    "datacenter": re.compile(r"centro de datos|data\s*center|centro de proceso de datos|\bCPD\b", re.I),
    "solar": re.compile(r"fotovoltaic|huerto solar|planta solar", re.I),
}
# Capacity like "50 MW", "36,5 MWp", "1.234,5 MW". Spanish decimals: comma is the
# decimal separator, dot the thousands separator. Capture a full digit/separator
# run that both starts and ends with a digit (no trailing punctuation).
_MW_RE = re.compile(r"(\d[\d.,]*\d|\d)\s*MW(?:p|n|h|e)?\b", re.I)


class Classification(BaseModel):
    types: tuple[str, ...]      # any of: storage, wind, datacenter, solar
    max_mw: float | None        # largest MW figure found, or None


def _to_float(raw: str) -> float | None:
    s = raw.strip()
    try:
        if "," in s and "." in s:      # 1.234,5 -> 1234.5  (dot=thousands, comma=decimal)
            s = s.replace(".", "").replace(",", ".")
        elif "," in s:                  # 36,5 -> 36.5
            s = s.replace(",", ".")
        return float(s)
    except ValueError:
        return None


def classify(text: str) -> Classification:
    types = tuple(t for t, pat in _TYPE_PATTERNS.items() if pat.search(text))
    mws = [v for v in (_to_float(m) for m in _MW_RE.findall(text)) if v is not None]
    return Classification(types=types, max_mw=(max(mws) if mws else None))


def should_fetch_annexes(c: Classification, *, min_mw: float = DEFAULT_MIN_MW) -> tuple[bool, str]:
    """(fetch?, reason). `reason` is recorded in the linked_document state."""
    in_scope = [t for t in c.types if t in IN_SCOPE]
    if not in_scope:
        if "solar" in c.types:
            return False, "solar_only"
        return False, ("out_of_scope" if c.types else "unclassified")
    if c.max_mw is None:
        return True, "in_scope_mw_unknown:" + "+".join(in_scope)
    if c.max_mw < min_mw:
        return False, f"below_min_mw:{c.max_mw}"
    return True, f"in_scope:{'+'.join(in_scope)}:{c.max_mw}MW"
