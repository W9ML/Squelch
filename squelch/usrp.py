"""USRP (chan_usrp) UDP audio ingest.

ASL's chan_usrp sends UDP datagrams with a 32-byte header (fields in
network byte order) optionally followed by 160 samples of 16-bit
little-endian signed PCM at 8 kHz (20 ms frames):

    char     eye[4]      "USRP"
    uint32   seq
    uint32   memory
    uint32   keyup       PTT state (1 while keyed; header-only frame
                         with keyup=0 marks unkey)
    uint32   talkgroup
    uint32   type        0=voice 1=dtmf 2=text
    uint32   mpxid
    uint32   reserved

The Segmenter turns the frame stream into discrete transmissions:
audio is buffered from key-up until either an explicit unkey frame or
a configurable gap with no audio.
"""

from __future__ import annotations

import asyncio
import logging
import struct
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import numpy as np

log = logging.getLogger(__name__)

HEADER = struct.Struct(">4sIIIIIII")
SAMPLE_RATE = 8000

USRP_TYPE_VOICE = 0
USRP_TYPE_DTMF = 1
USRP_TYPE_TEXT = 2


@dataclass
class USRPFrame:
    seq: int
    keyup: bool
    type: int
    talkgroup: int
    pcm: np.ndarray | None  # int16 array or None for header-only frames
    data: bytes = b""       # raw payload for non-voice frames (DTMF keys)


def parse_frame(datagram: bytes) -> USRPFrame | None:
    if len(datagram) < HEADER.size:
        return None
    eye, seq, _memory, keyup, talkgroup, ftype, _mpxid, _reserved = \
        HEADER.unpack_from(datagram)
    if eye != b"USRP":
        return None
    pcm = None
    payload = datagram[HEADER.size:]
    if ftype == USRP_TYPE_VOICE and len(payload) >= 2:
        pcm = np.frombuffer(payload[:len(payload) & ~1], dtype="<i2")
    return USRPFrame(seq=seq, keyup=bool(keyup), type=ftype,
                     talkgroup=talkgroup, pcm=pcm,
                     data=payload if ftype == USRP_TYPE_DTMF else b"")


_DTMF_CHARS = frozenset("0123456789ABCD*#")


def _dtmf_digits(payload: bytes) -> list[str]:
    """chan_usrp TYPE_DTMF frames carry the pressed key(s) as ASCII
    (implementations vary between one key per frame and a NUL-padded
    buffer, so parse defensively)."""
    text = payload.split(b"\x00", 1)[0].decode("ascii", "ignore")
    return [c for c in text.upper() if c in _DTMF_CHARS]


@dataclass
class Transmission:
    started_at: float
    ended_at: float
    audio: np.ndarray  # int16 @ 8 kHz
    # key presses chan_usrp decoded upstream (TYPE_DTMF frames):
    # [{"d": digit, "t": seconds-into-tx}, ...]
    dtmf: list = field(default_factory=list)

    @property
    def duration_ms(self) -> int:
        return int(len(self.audio) * 1000 / SAMPLE_RATE)


def _log_task_exc(task: "asyncio.Task") -> None:
    """Done-callback for fire-and-forget segmenter dispatches. Without it a
    transient fault in on_complete (a disk-full or DB-lock in _save_wav, say)
    drops the whole over with nothing but a GC-time 'Task exception was never
    retrieved'. On a monitor, silent total loss while looking healthy is the
    nasty case — so surface it in the log."""
    try:
        exc = task.exception()
    except asyncio.CancelledError:
        return
    if exc is not None:
        log.error("segmenter dispatch failed", exc_info=exc)


def _spawn(coro) -> None:
    asyncio.ensure_future(coro).add_done_callback(_log_task_exc)


class Segmenter:
    """Accumulates voice frames into transmissions.

    on_start() fires when a transmission begins (for the live RX
    indicator); on_complete(tx) fires when it ends. Both are async
    callbacks invoked on the event loop.

    live_audio, if provided, receives each raw PCM chunk in real time so
    connected browsers can hear transmissions as they happen.
    """

    def __init__(self,
                 on_start: Callable[[], Awaitable[None]],
                 on_complete: Callable[[Transmission], Awaitable[None]],
                 on_discard: Callable[[], Awaitable[None]] | None = None,
                 squelch_tail_ms: int = 400,
                 min_tx_ms: int = 300,
                 max_tx_secs: int = 300,
                 live_audio=None):
        self.on_start = on_start
        self.on_complete = on_complete
        self.on_discard = on_discard
        self.live_audio = live_audio
        self.squelch_tail_s = squelch_tail_ms / 1000.0
        self.min_tx_ms = min_tx_ms
        self.max_tx_samples = max_tx_secs * SAMPLE_RATE
        self.active = False
        self.last_frame_mono = 0.0
        self.last_frame_wall = 0.0  # any USRP traffic, for link status
        self._chunks: list[np.ndarray] = []
        self._nsamples = 0
        self._dtmf: list[dict] = []
        self._started_at = 0.0
        self._watchdog: asyncio.Task | None = None

    def feed(self, frame: USRPFrame) -> None:
        self.last_frame_wall = time.time()
        if frame.type == USRP_TYPE_DTMF:
            # node-decoded key press, delivered out-of-band alongside the
            # audio: record it on the current tx and surface it live
            for d in _dtmf_digits(frame.data):
                if self.active:
                    self._dtmf.append(
                        {"d": d, "t": round(self._nsamples / SAMPLE_RATE, 2)})
                if self.live_audio is not None:
                    send = getattr(self.live_audio, "send_dtmf_soon", None)
                    if send is not None:
                        send(d)
            return
        if frame.type != USRP_TYPE_VOICE:
            return
        now = time.monotonic()
        if frame.pcm is not None and len(frame.pcm):
            if not self.active:
                self.active = True
                self._chunks = []
                self._nsamples = 0
                self._dtmf = []
                self._started_at = time.time()
                _spawn(self.on_start())
                if self.live_audio:
                    self.live_audio.send_start_soon()
            self._chunks.append(frame.pcm)
            self._nsamples += len(frame.pcm)
            self.last_frame_mono = now
            if self.live_audio:
                self.live_audio.send_frame_soon(frame.pcm.tobytes())
            if self._nsamples >= self.max_tx_samples:
                self._finish()
        elif self.active and not frame.keyup:
            # header-only frame with keyup=0: explicit unkey
            self._finish()

    def _finish(self) -> None:
        if not self.active:
            return
        self.active = False
        audio = np.concatenate(self._chunks) if self._chunks else \
            np.zeros(0, dtype=np.int16)
        self._chunks = []
        dtmf, self._dtmf = self._dtmf, []
        if self.live_audio:
            self.live_audio.send_end_soon()
        tx = Transmission(started_at=self._started_at,
                          ended_at=time.time(), audio=audio, dtmf=dtmf)
        if tx.duration_ms < self.min_tx_ms:
            log.debug("discarding %d ms kerchunk", tx.duration_ms)
            if self.on_discard is not None:
                _spawn(self.on_discard())
            return
        _spawn(self.on_complete(tx))

    async def run_watchdog(self) -> None:
        """Ends a transmission after squelch_tail with no frames (covers
        setups where chan_usrp never sends an explicit unkey)."""
        while True:
            await asyncio.sleep(0.1)
            if self.active and \
                    time.monotonic() - self.last_frame_mono > self.squelch_tail_s:
                self._finish()


class USRPProtocol(asyncio.DatagramProtocol):
    def __init__(self, segmenter: Segmenter):
        self.segmenter = segmenter

    def datagram_received(self, data: bytes, addr) -> None:
        frame = parse_frame(data)
        if frame is not None:
            self.segmenter.feed(frame)


async def start_listener(bind: str, port: int, segmenter: Segmenter):
    loop = asyncio.get_running_loop()
    transport, _protocol = await loop.create_datagram_endpoint(
        lambda: USRPProtocol(segmenter), local_addr=(bind, port))
    log.info("USRP listener on %s:%d", bind, port)
    return transport
