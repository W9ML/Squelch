"""Ingest MDC-1200 IDs decoded by app_rpt on the node.

On many repeaters the MDC pre-burst is gated out of the audio before it
reaches squelch (receiver squelch/COS timing), so squelch's own audio
decoder never sees it. But app_rpt's decoder taps the raw receiver
audio and logs every burst. A small forwarder on the node ships those
to POST /api/mdc; this module matches each one to the transmission it
belongs to, by time.

Matching uses squelch's own clock for both sides: the transmission
timestamps and the arrival time of the forwarded event are stamped by
squelch, so no clock sync between the node and the VM is required (the
forwarder ships each burst within ~1-2 s, and only forwards new lines).

A pre-burst is decoded at the *start* of a transmission, which usually
arrives here while the transmission is still keyed — before its DB
record exists (records are created at unkey). So unmatched events are
buffered and attached when the transmission is created; late/post
bursts are attached to an already-created transmission on arrival.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# how far outside a transmission's [start, end] an event may fall and
# still be considered part of it (seconds)
PRE_MARGIN = 6.0
POST_MARGIN = 4.0
BUFFER_TTL = 120.0

# app_rpt mdclog line: "YYYYMMDDhhmmss <node> <TYPE><unitid>"
#   TYPE: I=PTT ID, E=Emergency, S=Status, C=Call
_LOG_RE = re.compile(r"^(\d{14})\s+(\d+)\s+([IESC])(\S+)\s*$")

_TYPE_LABEL = {
    "I": "PTT ID",
    "E": "Emergency",
    "S": "Status",
    "C": "Call",
}


def parse_log_line(line: str) -> dict | None:
    """Parse one app_rpt mdclog line into an event dict, or None."""
    m = _LOG_RE.match(line.strip())
    if not m:
        return None
    _ts, node, mtype, unit = m.groups()
    return {"node": node, "type": mtype, "unit": unit}


def make_entry(mtype: str, unit: str, node: str | None = None) -> dict:
    """Build the MDC entry stored on a transmission (shape kept close to
    audio-decoded MDCPacket.to_dict, plus source/unit_raw for display)."""
    try:
        unit_id = int(unit)
    except ValueError:
        unit_id = None
    entry = {
        "source": "node",
        "type": mtype,
        "label": _TYPE_LABEL.get(mtype, f"MDC {mtype}"),
        "unit_raw": str(unit),
        "unit_id": unit_id,
    }
    if node:
        entry["node"] = node
    return entry


@dataclass
class _Buffered:
    recv: float
    entry: dict


class MDCMatcher:
    """Attaches forwarded MDC events to transmissions by time.

    Thread-affinity: all methods run on the event loop. DB access is
    offloaded with asyncio.to_thread by the caller-facing coroutines.
    """

    def __init__(self, db, broadcaster, rx_active=None):
        self.db = db
        self.broadcaster = broadcaster
        self._rx_active = rx_active or (lambda: False)
        self._buffer: list[_Buffered] = []

    async def ingest(self, entry: dict, recv: float | None = None) -> bool:
        """Handle one forwarded MDC event. Returns True if it was
        attached to an existing transmission immediately, False if it
        was buffered for a not-yet-created one."""
        recv = recv if recv is not None else time.time()
        self._prune(recv)

        if self._rx_active():
            # burst decoded during active reception belongs to the
            # in-flight transmission, not a recently-ended one
            self._buffer.append(_Buffered(recv=recv, entry=entry))
            return False

        tx_id = await asyncio.to_thread(
            self.db.find_tx_for_time, recv, PRE_MARGIN, POST_MARGIN)
        if tx_id is not None:
            await self._attach(tx_id, [entry])
            return True

        self._buffer.append(_Buffered(recv=recv, entry=entry))
        return False

    async def on_tx_created(self, tx_id: int, started_at: float,
                            ended_at: float) -> list[dict]:
        """Drain buffered events belonging to a just-created
        transmission. Returns the entries attached (so the caller can
        include them in the first broadcast)."""
        lo = started_at - PRE_MARGIN
        hi = ended_at + POST_MARGIN
        keep, take = [], []
        for b in self._buffer:
            (take if lo <= b.recv <= hi else keep).append(b)
        self._buffer = keep
        if not take:
            return []
        entries = [b.entry for b in take]
        await asyncio.to_thread(self.db.append_mdc, tx_id, entries)
        return entries

    async def _attach(self, tx_id: int, entries: list[dict]) -> None:
        await asyncio.to_thread(self.db.append_mdc, tx_id, entries)
        record = await asyncio.to_thread(self.db.get_transmission, tx_id)
        # a late event lands after the pipeline's ML stage already ran its
        # MDC→operator check, so apply it here too — otherwise a mapped
        # PTT ID attaches the badge but never names the speaker. Only fills
        # a gap: manual/mdc/callsign assignments are stronger and kept.
        if record and not record.get("speaker_verified"):
            units = [m.get("unit_raw") for m in (record.get("mdc") or [])
                     if m.get("type") == "I" and m.get("unit_raw")]
            owner = (await asyncio.to_thread(self.db.mdc_operator_for, units)
                     if units else None)
            if owner is not None and owner != record.get("speaker_id"):
                await asyncio.to_thread(
                    self.db.assign_speaker, tx_id, owner, None, "mdc")
                record = await asyncio.to_thread(self.db.get_transmission, tx_id)
        if record:
            await self.broadcaster.send("tx_update", {"tx": record})
        log.info("attached MDC %s to tx %d",
                 [e.get("unit_raw") for e in entries], tx_id)

    def _prune(self, now: float) -> None:
        self._buffer = [b for b in self._buffer if now - b.recv < BUFFER_TTL]
