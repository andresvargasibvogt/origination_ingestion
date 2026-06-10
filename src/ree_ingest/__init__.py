"""Monthly REE capacity-file poller.

Watches Red Eléctrica's "Conoce la capacidad de acceso" page for the CSV
"Capacidad de acceso de generación disponible y ocupada en los nudos de la
red de transporte" and lands each new monthly release in OneLake bronze.

Unlike the gazette sources (BOE/BOA) this is a poll-for-new-version pattern,
not a daily sweep: the cron runs daily but the orchestrator no-ops until a
new publication appears (detected by the publication date encoded in the
filename, deduped against what's already in OneLake).
"""

from __future__ import annotations

__version__ = "0.1.0"
