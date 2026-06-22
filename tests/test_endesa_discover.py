"""e-distribución landing-page link discovery tests.

The discovery is load-bearing: the page carries TWO parallel monthly CSV series
("e-distribución" wanted, "EASA" excluded) with near-identical filenames, the
filename suffix drifts between `generación.csv` and `generacion.csv`, and each
href appears in two anchors (a descriptive one and a "CSV(NNN)KB" one). These
tests pin that behavior against snippets modeled on the real 2026-06 page.
"""

from __future__ import annotations

from datetime import date

import pytest

from endesa_ingest.discover import DiscoveryError, find_latest_csv

_DAM = "/content/dam/edistribucion/conexion-a-la-red/descargables/nodos/generacion"

# Two series for June (e-distribución R1299 + EASA R1026), each with a
# descriptive anchor AND a "CSV(NNN)KB" anchor and an XLSX twin — exactly the
# real structure. Plus an older May entry with the NON-accented spelling.
_PAGE = f"""
<ul>
  <li><a href="{_DAM}/202606/2026_06_03_R1299_generación.csv">Capacidad de generación en e-distribución junio 2026 csv</a>
      <a href="{_DAM}/202606/2026_06_03_R1299_generación.csv">CSV(267)KB</a>
      <a href="{_DAM}/202606/2026_06_03_R1299_generación.xlsx">Capacidad de generación en e-distribución junio 2026 xlsx</a></li>
  <li><a href="{_DAM}/202606/2026_06_03_R1026_generación.csv">Capacidad de generación en EASA junio 2026 csv</a>
      <a href="{_DAM}/202606/2026_06_03_R1026_generación.csv">CSV(1)KB</a></li>
  <li><a href="{_DAM}/202605/2026_05_05_R1299_generacion.csv">Capacidad de generación en e-distribución mayo 2026 csv</a>
      <a href="{_DAM}/202605/2026_05_05_R1026_generacion.csv">Capacidad de generación en EASA mayo 2026 csv</a></li>
</ul>
"""


def test_selects_edistribucion_series_and_parses_date() -> None:
    latest = find_latest_csv(_PAGE)
    assert latest.filename == "2026_06_03_R1299_generación.csv"
    assert latest.published_at == date(2026, 6, 3)
    assert latest.url_path == f"{_DAM}/202606/2026_06_03_R1299_generación.csv"


def test_never_selects_the_easa_series() -> None:
    # The wanted series is R1299; the EASA R1026 file must never be chosen,
    # even when it shares the same publication date.
    latest = find_latest_csv(_PAGE)
    assert "R1026" not in latest.filename
    assert "EASA" not in latest.filename


def test_picks_most_recent_edistribucion_month() -> None:
    latest = find_latest_csv(_PAGE)
    assert latest.published_at == date(2026, 6, 3)  # June over May


def test_tolerates_non_accented_filename() -> None:
    # Older months spell it `generacion.csv` (no accent) — still discoverable.
    html = f"""
      <a href="{_DAM}/202509/2025_09_09_R1299_generacion.csv">Capacidad de generación en e-distribución septiembre 2025 csv</a>
      <a href="{_DAM}/202509/2025_09_09_R1026_generacion.csv">Capacidad de generación en EASA septiembre 2025 csv</a>
    """
    latest = find_latest_csv(html)
    assert latest.filename == "2025_09_09_R1299_generacion.csv"
    assert latest.published_at == date(2025, 9, 9)


def test_ignores_xlsx_twin() -> None:
    html = f"""
      <a href="{_DAM}/202606/2026_06_03_R1299_generación.xlsx">Capacidad de generación en e-distribución junio 2026 xlsx</a>
      <a href="{_DAM}/202605/2026_05_05_R1299_generación.csv">Capacidad de generación en e-distribución mayo 2026 csv</a>
    """
    # Only May has a CSV; the June XLSX must not be selected.
    latest = find_latest_csv(html)
    assert latest.filename == "2026_05_05_R1299_generación.csv"


def test_raises_when_only_easa_series_present() -> None:
    html = f'<a href="{_DAM}/202606/2026_06_03_R1026_generación.csv">Capacidad de generación en EASA junio 2026 csv</a>'
    with pytest.raises(DiscoveryError):
        find_latest_csv(html)
