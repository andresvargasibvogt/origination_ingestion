"""Shared infrastructure for the origination ingestion pipeline.

Source-agnostic building blocks reused by every source package
(boe_ingest, boa_ingest, ree_ingest, ...):

  - manifest   : Pydantic manifest schemas + Source literal + attribution
  - onelake    : OneLakeWriter / LocalWriter / Writer protocol / credential selection
  - blob       : BlobWriter for the Defender-scanned staging account
  - fetcher    : async HTTP fetcher (httpx + tenacity)
  - robots     : RobotsGuard
  - paths      : bronze/{source}/raw/... path helpers
  - promoter   : staging → OneLake promoter (handles any source from the path)

No source-specific logic lives here. A source package owns only its discovery
(sumario/SPA/landing-page), its relevance filter, its orchestrator, and its CLI.
"""

from __future__ import annotations

__version__ = "0.3.0"
