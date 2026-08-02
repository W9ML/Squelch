"""Attach traffic-origin events from the node to transmissions.

A small monitor on the node (tools/source_monitor.py) polls app_rpt via
AMI and reports, at each key-up, whether the audio source is the node's own
receiver ("local") or a connected node (its node number). Events are
matched to transmissions by arrival time on squelch's clock, with the
same buffer-until-created trick as MDC ingest: key-up events arrive
while the transmission is still in progress, before its DB record
exists.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

PRE_MARGIN = 6.0
POST_MARGIN = 4.0
BUFFER_TTL = 120.0


@dataclass
class _Buffered:
    recv: float
    origin: str
    hub: str | None = None


class SourceMatcher:
    def __init__(self, db, broadcaster, rx_active=None):
        self.db = db
        self.broadcaster = broadcaster
        self._rx_active = rx_active or (lambda: False)
        self._buffer: list[_Buffered] = []

    async def ingest(self, origin: str, hub: str | None = None,
                     recv: float | None = None) -> bool:
        recv = recv if recv is not None else time.time()
        self._prune(recv)
        # always retain the event: one node key-up can span several segmented
        # overs (a roundtable where the far end holds the key across squelch
        # gaps), so it must stay available to every over in that window
        self._buffer.append(_Buffered(recv=recv, origin=origin, hub=hub))
        if self._rx_active():
            # a key-up event during active reception belongs to the
            # in-flight transmission (whose record doesn't exist yet) —
            # never attach it backwards to an already-ended one
            return False
        tx_id = await asyncio.to_thread(
            self.db.find_tx_for_time, recv, PRE_MARGIN, POST_MARGIN)
        if tx_id is not None:
            changed = await asyncio.to_thread(
                self.db.set_origin_if_empty, tx_id, origin, hub)
            if changed:
                record = await asyncio.to_thread(self.db.get_transmission, tx_id)
                if record:
                    await self.broadcaster.send("tx_update", {"tx": record})
            return True
        return False

    async def on_tx_created(self, tx_id: int, started_at: float,
                            ended_at: float) -> None:
        # the over's source is the key-up event closest to when the over
        # started (the node monitor and the audio segmenter observe the same
        # key-up with a bit of jitter in both directions). Events are NOT
        # consumed — deleting them let an earlier over eat the key-up that
        # belonged to the next over in a busy roundtable, dropping its origin.
        lo, hi = started_at - PRE_MARGIN, started_at + POST_MARGIN
        cands = [b for b in self._buffer if lo <= b.recv <= hi]
        if cands:
            pick = min(cands, key=lambda b: abs(b.recv - started_at))
            await asyncio.to_thread(
                self.db.set_origin_if_empty, tx_id, pick.origin, pick.hub)
        # age-based prune only; matched events remain for adjacent overs
        self._buffer = [b for b in self._buffer if ended_at - b.recv < BUFFER_TTL]

    def _prune(self, now: float) -> None:
        self._buffer = [b for b in self._buffer if now - b.recv < BUFFER_TTL]
