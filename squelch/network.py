"""Live per-hub node/link status, from the hubs' source monitors.

Each hub runs tools/source_monitor.py, which POSTs /api/link every ~30s (a
heartbeat, plus immediately on any change) with the nodes it's linked to. This
keeps a PER-HUB view — the three inter-linked TACS hubs would otherwise clobber
each other in one global set (which is all voter gating needs, but useless for a
roster). Exposed publicly at /api/network: the same node-connection data
AllStarLink already publishes at allstarlink.org/nodelist.

Design notes:
- Online is computed at READ time from heartbeat staleness, so a hub going dark
  needs no background task to read as offline — the next snapshot is correct.
- `since` is when the hub was first seen (in-memory; resets if the app restarts).
  It is labelled honestly as "linked since observed", NOT fabricated uptime.
"""
from __future__ import annotations

import threading
import time

# a hub with no heartbeat in this long reads as offline. The monitor beats
# every ~30s, so this tolerates a couple of missed reports before flipping.
HUB_TTL = 75.0


class NetworkState:
    """Thread-safe map of hub -> its currently-linked nodes + last heartbeat."""

    def __init__(self) -> None:
        self._hubs: dict[str, dict] = {}
        self._lock = threading.Lock()

    def report(self, hub: str | None, nodes, ts: float | None = None) -> None:
        """Record a hub's heartbeat + connected-node list. No-op if `hub` is
        absent (an un-enriched monitor that only sends the flat list) — a
        per-hub view is impossible without knowing which hub reported."""
        if not hub:
            return
        hub = str(hub).strip()
        ts = ts if ts is not None else time.time()
        clean = sorted({str(n).strip() for n in (nodes or []) if str(n).strip()})
        with self._lock:
            cur = self._hubs.get(hub)
            # keep the first-seen anchor across heartbeats; reset only after a
            # real gap (hub was stale / never seen)
            first = cur["first_seen"] if cur and (ts - cur["reported_at"]) < HUB_TTL else ts
            self._hubs[hub] = {"nodes": clean, "reported_at": ts, "first_seen": first}

    def snapshot(self, now: float | None = None) -> list[dict]:
        now = now if now is not None else time.time()
        with self._lock:
            return [
                {
                    "hub": hub,
                    "online": (now - s["reported_at"]) < HUB_TTL,
                    "reported_at": s["reported_at"],
                    "since": s["first_seen"],
                    "nodes": list(s["nodes"]),
                }
                for hub, s in sorted(self._hubs.items())
            ]
