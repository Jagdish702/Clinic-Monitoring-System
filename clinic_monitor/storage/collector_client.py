"""
Best-effort fan-in to a central collector, for multi-VM deployments.

Every patrol VM keeps writing to its own local database exactly as it always
has - this module only adds a second, non-blocking copy of each write sent
to one shared dashboard's collector, so many clusters can appear on a single
dashboard without a shared network database.

``config.COLLECTOR_URL`` unset means single-VM mode: :func:`push` becomes a
no-op and nothing here is ever imported into a network call, so a deployment
that never sets it behaves exactly as it did before this module existed.

Failure here must never cost a local write. A patrol has already committed
the row to its own database by the time this runs - a slow or unreachable
collector is a missed copy elsewhere, not a reason to slow down or crash the
patrol that is still visiting the next clinic.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

import requests

import config

log = logging.getLogger(__name__)


def push(kind: str, payload: Dict[str, Any]) -> None:
    """
    Send one write to the central collector, if one is configured.

    ``kind`` is the collector route to hit - "events", "observations" or
    "clinic_status" - matching the three things storage/database.py writes.
    """
    if not config.COLLECTOR_URL:
        return
    try:
        resp = requests.post(
            f"{config.COLLECTOR_URL}/collect/{kind}",
            json=payload,
            headers={"Authorization": f"Bearer {config.COLLECTOR_TOKEN}"},
            timeout=config.COLLECTOR_TIMEOUT_SEC,
        )
        if resp.status_code >= 400:
            log.warning(
                "collector rejected %s push: %s %s", kind, resp.status_code, resp.text[:200]
            )
    except requests.RequestException as exc:
        # Never let a network problem elsewhere stop this VM's own patrol -
        # the row is already safe in the local database either way.
        log.debug("collector push failed (%s): %s", kind, exc)
