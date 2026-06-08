"""Daily BOA scraper landing raw PDFs in lh_esp_origination (Fabric OneLake).

Sibling package to `boe_ingest`. Shares the staging→Defender→promoter→OneLake
pipeline; differs in the discovery mechanism (Angular SPA, scraped via
Playwright) and the filter shape (section + subsection + departamento_name).
"""

from __future__ import annotations

__version__ = "0.1.0"
