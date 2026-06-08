"""robots.txt guard for boe.es.

Honors the disallow rules — `verifica.*`, `/boe_n/`, `/notificaciones/`,
`/boe_j/`, `/edictos_judiciales/`, plus the ~hundreds of individual
'derecho al olvido' blocked document IDs (deep-dive §8).

The relevance filter already excludes those sections at the corpus level,
but this guard is belt-and-suspenders: any URL we're about to fetch is
checked against robots first. Blocked URLs are silently skipped (logged
as `robots_blocked`) and counted in the manifest — they're not failures.
"""

from __future__ import annotations

from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx
import structlog

from .config import BOE_BASE_URL

log = structlog.get_logger()

_DEFAULT_USER_AGENT = "boe-ingest/1.0"


class RobotsGuard:
    """Lazy-loaded robots.txt evaluator.

    `load()` must be called once before `can_fetch()` is used. We do this
    explicitly (not in __init__) so callers can control when network I/O
    happens — important for testing.
    """

    def __init__(
        self,
        base_url: str = BOE_BASE_URL,
        user_agent: str = _DEFAULT_USER_AGENT,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._user_agent = user_agent
        self._parser = RobotFileParser()
        self._loaded = False

    def load(self, client: httpx.Client | None = None) -> None:
        """Fetch and parse robots.txt. Idempotent."""
        if self._loaded:
            return
        url = f"{self._base_url}/robots.txt"
        if client is None:
            with httpx.Client(headers={"User-Agent": self._user_agent}) as c:
                resp = c.get(url)
        else:
            resp = client.get(url)
        resp.raise_for_status()
        self._parser.parse(resp.text.splitlines())
        self._loaded = True
        log.info("robots_loaded", url=url, lines=len(resp.text.splitlines()))

    def can_fetch(self, url: str) -> bool:
        if not self._loaded:
            raise RuntimeError("RobotsGuard.load() must be called before can_fetch()")
        # robotparser expects the full URL; honor the configured user agent.
        return self._parser.can_fetch(self._user_agent, url)

    def assert_same_host(self, url: str) -> None:
        """Sanity check: only check URLs on the base host.

        Avoids false negatives if someone accidentally passes an off-host URL.
        """
        host = urlparse(url).netloc
        base_host = urlparse(self._base_url).netloc
        if host and host != base_host:
            raise ValueError(f"URL {url!r} is not on host {base_host!r}")
