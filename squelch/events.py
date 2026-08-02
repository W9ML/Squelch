"""WebSocket event broadcasting."""

from __future__ import annotations

import asyncio
import json
import logging

log = logging.getLogger(__name__)

# fields on a broadcast transmission that only authenticated users may see.
# Voter RSSI is login-gated (it feeds the admin-only geolocation), so it's
# stripped from the live feed for anonymous sockets — mirroring the same gate
# on the /api/transmissions REST payloads.
_SENSITIVE_TX_FIELDS: tuple[str, ...] = ("voter",)


def _sanitize(payload: dict | None) -> dict | None:
    """Return a copy of a broadcast payload with sensitive transmission
    fields removed, or the same object unchanged if there's nothing to
    strip (so the common case serializes only once)."""
    if not payload:
        return payload
    tx = payload.get("tx")
    if not isinstance(tx, dict) or not any(f in tx for f in _SENSITIVE_TX_FIELDS):
        return payload
    clean = {k: v for k, v in tx.items() if k not in _SENSITIVE_TX_FIELDS}
    return {**payload, "tx": clean}


class Broadcaster:
    def __init__(self):
        # ws -> (is_admin, username, can_settings). is_admin means "logged
        # in" (gates voter redaction); can_settings means super/admin role
        # (gates the connection-count presence events).
        self._clients: dict = {}

    def add(self, ws, is_admin: bool = False, username: str | None = None,
            can_settings: bool = False) -> None:
        self._clients[ws] = (is_admin, username, can_settings)

    def remove(self, ws) -> None:
        self._clients.pop(ws, None)

    @property
    def client_count(self) -> int:
        return len(self._clients)

    async def broadcast_presence(self) -> None:
        """Push the live connection count to super/admin sockets only — the
        counter is an admin feature (plain users and anonymous clients never
        receive it). Called on every /ws connect and disconnect."""
        msg = json.dumps({"type": "presence", "count": len(self._clients)})
        dead = []
        for ws, (_is_admin, _user, can_settings) in list(self._clients.items()):
            if not can_settings:
                continue
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._clients.pop(ws, None)

    async def send(self, event_type: str, payload: dict | None = None) -> None:
        if not self._clients:
            return
        full = json.dumps({"type": event_type, **(payload or {})})
        safe_payload = _sanitize(payload)
        safe = full if safe_payload is payload else json.dumps(
            {"type": event_type, **(safe_payload or {})})
        dead = []
        for ws, (is_admin, _user, _cs) in list(self._clients.items()):
            try:
                await ws.send_text(full if is_admin else safe)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._clients.pop(ws, None)

    async def send_to_user(self, username: str,
                           event_type: str, payload: dict | None = None) -> None:
        """Deliver an event only to the given user's sockets — used for
        per-user alerts so one operator's watch never fires on another's
        screen. Watch owners are logged-in (admin) sockets, so no sanitize."""
        if not username or not self._clients:
            return
        full = json.dumps({"type": event_type, **(payload or {})})
        dead = []
        for ws, (_is_admin, user, _cs) in list(self._clients.items()):
            if user != username:
                continue
            try:
                await ws.send_text(full)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._clients.pop(ws, None)

    def send_soon(self, event_type: str, payload: dict | None = None) -> None:
        asyncio.ensure_future(self.send(event_type, payload))


class AudioTee:
    """Fans the Segmenter's live_audio hook out to several sinks — the
    browser PCM broadcaster, the live DTMF detector, the live captioner.
    Implements the same duck-typed interface the Segmenter expects; sinks
    without send_dtmf_soon simply don't get DTMF."""

    def __init__(self, *sinks):
        self._sinks = [s for s in sinks if s is not None]

    def send_start_soon(self) -> None:
        for s in self._sinks:
            s.send_start_soon()

    def send_frame_soon(self, pcm_bytes: bytes) -> None:
        for s in self._sinks:
            s.send_frame_soon(pcm_bytes)

    def send_end_soon(self) -> None:
        for s in self._sinks:
            s.send_end_soon()

    def send_dtmf_soon(self, digit: str) -> None:
        for s in self._sinks:
            fn = getattr(s, "send_dtmf_soon", None)
            if fn is not None:
                fn(digit)


class LiveAudioBroadcaster:
    """Streams raw int16 PCM chunks to /ws/audio clients in real time.

    Control frames are JSON text; audio chunks are binary (int16 LE PCM).
    Protocol:
      server→client  {"type":"start","sample_rate":8000}  — TX begins
      server→client  <bytes>                               — 20 ms PCM chunk
      server→client  {"type":"end"}                        — TX ends
    """

    SAMPLE_RATE = 8000

    def __init__(self):
        self._clients: set = set()

    def add(self, ws) -> None:
        self._clients.add(ws)

    def remove(self, ws) -> None:
        self._clients.discard(ws)

    @property
    def has_listeners(self) -> bool:
        return bool(self._clients)

    async def _text(self, msg: str) -> None:
        dead = []
        for ws in list(self._clients):
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._clients.discard(ws)

    async def send_start(self) -> None:
        if self._clients:
            await self._text(
                json.dumps({"type": "start", "sample_rate": self.SAMPLE_RATE}))

    async def send_frame(self, pcm_bytes: bytes) -> None:
        if not self._clients:
            return
        dead = []
        for ws in list(self._clients):
            try:
                await ws.send_bytes(pcm_bytes)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._clients.discard(ws)

    async def send_end(self) -> None:
        if self._clients:
            await self._text(json.dumps({"type": "end"}))

    def send_start_soon(self) -> None:
        asyncio.ensure_future(self.send_start())

    def send_frame_soon(self, pcm_bytes: bytes) -> None:
        if self._clients:
            asyncio.ensure_future(self.send_frame(pcm_bytes))

    def send_end_soon(self) -> None:
        asyncio.ensure_future(self.send_end())
