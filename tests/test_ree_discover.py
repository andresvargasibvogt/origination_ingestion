"""REE landing-page link discovery tests.

The discovery regex is the load-bearing piece for REE: it must pull the
correct `*_GRT_generacion.csv` href out of the page HTML and pick the most
recent publication date. These tests pin that behavior against representative
HTML snippets (taken from the real 2026-06 landing page structure).
"""

from __future__ import annotations

from datetime import date

import pytest

from ree_ingest.discover import DiscoveryError, find_latest_csv

# Mirrors the real page: CSV + PDF + XLSX of the same release, plus an
# unrelated document. Only the CSV should be selected.
_PAGE_SNIPPET = """
<ul>
  <li><a href="/sites/default/files/12_CLIENTES/Documentos/2026_06_04_GRT_generacion.pdf">Capacidad (PDF)</a></li>
  <li><a href="/sites/default/files/12_CLIENTES/Documentos/2026_06_04_GRT_generacion.csv">Capacidad (CSV)</a></li>
  <li><a href="/sites/default/files/12_CLIENTES/Documentos/2026_06_04_GRT_generacion.xlsx">Capacidad (XLSX)</a></li>
  <li><a href="/sites/default/files/12_CLIENTES/Documentos/2026_06_01_Generacion_por_posicion.pdf">Otra cosa</a></li>
</ul>
"""


def test_finds_the_csv_and_parses_date() -> None:
    latest = find_latest_csv(_PAGE_SNIPPET)
    assert latest.filename == "2026_06_04_GRT_generacion.csv"
    assert latest.published_at == date(2026, 6, 4)
    assert latest.url_path == "/sites/default/files/12_CLIENTES/Documentos/2026_06_04_GRT_generacion.csv"


def test_picks_most_recent_when_multiple_months_present() -> None:
    html = """
      <a href="/sites/default/files/12_CLIENTES/Documentos/2026_04_01_GRT_generacion.csv">old</a>
      <a href="/sites/default/files/12_CLIENTES/Documentos/2026_06_04_GRT_generacion.csv">new</a>
      <a href="/sites/default/files/12_CLIENTES/Documentos/2026_05_04_GRT_generacion.csv">mid</a>
    """
    latest = find_latest_csv(html)
    assert latest.published_at == date(2026, 6, 4)


def test_ignores_pdf_and_xlsx_variants() -> None:
    html = """
      <a href="/sites/default/files/12_CLIENTES/Documentos/2026_07_02_GRT_generacion.pdf">pdf</a>
      <a href="/sites/default/files/12_CLIENTES/Documentos/2026_07_02_GRT_generacion.xlsx">xlsx</a>
      <a href="/sites/default/files/12_CLIENTES/Documentos/2026_06_04_GRT_generacion.csv">csv</a>
    """
    # The only CSV is June's, even though a July PDF/XLSX exist.
    latest = find_latest_csv(html)
    assert latest.filename == "2026_06_04_GRT_generacion.csv"


def test_raises_when_no_csv_present() -> None:
    html = '<a href="/sites/default/files/12_CLIENTES/Documentos/UmbralesWSCR_NudosRdT.xlsx">x</a>'
    with pytest.raises(DiscoveryError):
        find_latest_csv(html)
