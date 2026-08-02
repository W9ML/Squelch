"""Streaming live captions — closed captions for the repeater.

While a transmission is in progress, LiveCaptioner re-transcribes the
growing audio buffer every `captions_interval` seconds using its OWN
(small) whisper model — never the pipeline's Transcriber, whose
ensure_model() would swap the main model out from under it — and
broadcasts a 'caption' event carrying the full caption state:

    {"type": "caption", "text": <committed>, "pending": <tail>, "seq": n}

Words move from `pending` to `text` via local agreement: a word is
committed once two consecutive decodes of the growing audio agree on
it (LocalAgreement-2, the standard trick from whisper-streaming).
Committed text never changes; the pending tail fades in and may still
be revised. Everything here is ephemeral — the pipeline's full-model
transcript replaces it on the card when the transmission completes.

Long overs: decode cost is bounded by sliding the decode window
forward past audio whose words are already committed (with a little
overlap), so a 5-minute monologue never re-decodes from the top.
"""

from __future__ import annotations

import asyncio
import logging
import time

import numpy as np
from scipy.signal import resample_poly

from .config import Config
from .events import Broadcaster
from .transcribe import Transcriber
from .usrp import SAMPLE_RATE

log = logging.getLogger(__name__)

_MIN_AUDIO_S = 0.5       # first decode once this much audio has arrived —
                         # the sooner, the sooner the first (pending) words show
_POLL_S = 0.1
_SLIDE_OVERLAP_S = 0.3   # re-hear this much committed audio after a slide
_HARD_WINDOW_FACTOR = 1.5  # force a slide at this multiple of the window


def _norm(word: str) -> str:
    return "".join(c for c in word.lower() if c.isalnum())


def _lcp(a: list[str], b: list[str]) -> int:
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


class LiveCaptioner:
    """live_audio sink (see Segmenter) + one long-lived run() task."""

    def __init__(self, cfg: Config, broadcaster: Broadcaster,
                 transcriber: Transcriber | None = None):
        self.broadcaster = broadcaster
        self.model = cfg.captions_model
        self.interval = cfg.captions_interval
        self.max_window = int(cfg.captions_window_secs * SAMPLE_RATE)
        self.transcriber = transcriber or Transcriber(
            device=cfg.captions_device or cfg.whisper_device,
            compute_type=cfg.captions_compute or cfg.whisper_compute,
            language=cfg.language,
            download_root=cfg.data_dir / "models")
        self._active = False
        self._epoch = 0              # bumped on start/end; stale decodes drop
        self._chunks: list[np.ndarray] = []
        self._total = 0
        self._decoding = False
        self._seq = 0
        self._last_decode_mono = 0.0
        self._last_decoded_total = -1
        self._last_sent: tuple[str, str] | None = None
        self._start_wall = 0.0
        self._decode_count = 0
        self._first_logged = False
        self._reset_caption_state()

    def _reset_caption_state(self) -> None:
        self._base = 0               # window start (samples into the tx)
        self._committed: list[str] = []
        self._committed_end_s = 0.0  # absolute end time of last committed word
        # normalized UNCOMMITTED tail of the previous hypothesis — words
        # agree against this to get committed (LocalAgreement-2)
        self._prev_norm: list[str] = []

    # ---- live_audio sink interface (event-loop context) ----

    def send_start_soon(self) -> None:
        self._epoch += 1
        self._active = True
        self._chunks = []
        self._total = 0
        self._seq = 0
        self._last_decode_mono = 0.0
        self._last_decoded_total = -1
        self._last_sent = None
        self._start_wall = time.monotonic()   # key-up, for latency logging
        self._decode_count = 0
        self._first_logged = False
        self._reset_caption_state()

    def send_frame_soon(self, pcm_bytes: bytes) -> None:
        if not self._active:
            return
        arr = np.frombuffer(pcm_bytes, dtype="<i2")
        self._chunks.append(arr)
        self._total += len(arr)

    def send_end_soon(self) -> None:
        self._epoch += 1
        self._active = False
        self._chunks = []
        self._total = 0

    # ---- decode loop ----

    async def run(self) -> None:
        if not self.transcriber.available:
            log.info("live captions disabled: faster-whisper not installed")
            return
        while True:
            await asyncio.sleep(_POLL_S)
            try:
                await self._tick()
            except Exception:
                log.exception("live caption decode failed")
                self._decoding = False

    async def warmup(self) -> None:
        """Pay the one-time decode-init cost up front so the first real over
        doesn't. On a CPU model this first decode is many seconds (graph
        build + thread-pool spin-up); after it, decodes run at their steady
        rate. Waits for the model to load, then decodes a little silence."""
        if not self.transcriber.available:
            return
        for _ in range(600):                     # up to ~60 s for model load
            if self.transcriber.loaded_model == self.model:
                break
            await asyncio.sleep(0.1)
        try:
            # noise, not silence: silence early-exits on no-speech and never
            # warms the actual decode path. Match the live params (greedy, no
            # hotwords) so the warmed path is the one real overs use.
            rng = np.random.default_rng(0)
            for _ in range(3):
                noise = (rng.standard_normal(16000) * 0.05).astype(np.float32)
                await asyncio.to_thread(
                    self.transcriber.transcribe, noise, self.model, None, 1, False)
            log.info("live caption model warmed (%s)", self.model)
        except Exception:
            log.exception("caption warmup failed")

    async def _tick(self) -> None:
        if not self._active or self._decoding:
            return
        if self._total < _MIN_AUDIO_S * SAMPLE_RATE:
            return
        if self._total == self._last_decoded_total:
            return
        now = time.monotonic()
        if now - self._last_decode_mono < self.interval:
            return
        self._decoding = True
        try:
            epoch = self._epoch
            audio = np.concatenate(self._chunks)
            total = len(audio)
            self._slide(total)
            window = audio[self._base:]
            base_s = self._base / SAMPLE_RATE

            def work():
                f = window.astype(np.float32) / 32768.0
                w16 = resample_poly(f, 2, 1).astype(np.float32)
                # greedy (beam 1) and no hotwords: live captions are
                # provisional and get replaced by the beam-5, hotword-biased
                # large-v3 transcript on the card, so this is the right trade —
                # both make each decode dramatically cheaper (hotwords alone
                # cost 5-8x on CPU), so words appear sooner
                return self.transcriber.transcribe(
                    w16, self.model, beam_size=1, use_hotwords=False)

            t_dec0 = time.monotonic()
            text, words = await asyncio.to_thread(work)
            dec_ms = (time.monotonic() - t_dec0) * 1000.0
            if epoch != self._epoch:
                return                       # tx ended mid-decode: stale
            self._last_decode_mono = time.monotonic()
            self._last_decoded_total = total
            self._decode_count += 1
            log.debug("caption decode #%d: window %.1fs, %.0fms, text=%s",
                      self._decode_count, (total - self._base) / SAMPLE_RATE,
                      dec_ms, bool(text))
            if text is None:
                return                       # model still loading
            committed, pending = self._ingest(words, base_s)
            # None -> ("", "") so a first decode that heard nothing yet
            # doesn't broadcast an empty caption
            if (committed, pending) != (self._last_sent or ("", "")):
                if not self._first_logged and (committed or pending):
                    # one line per over: where did the time-to-first-words go?
                    # big window + many decodes => speech started late (dead
                    # air); big last-ms => decode cost / GPU contention
                    log.info(
                        "live caption: first words %.2fs after key-up "
                        "(window %.1fs, %d decodes, last %.0fms)",
                        time.monotonic() - self._start_wall,
                        (total - self._base) / SAMPLE_RATE,
                        self._decode_count, dec_ms)
                    self._first_logged = True
                self._last_sent = (committed, pending)
                await self.broadcaster.send(
                    "caption",
                    {"text": committed, "pending": pending, "seq": self._seq})
                self._seq += 1
        finally:
            self._decoding = False

    def _slide(self, total: int) -> None:
        """Advance the decode window when it grows beyond max_window —
        but only past audio whose words are already COMMITTED (dropping
        uncommitted audio would lose words), plus a hard cap that forces
        progress even when agreement is stalling so decode cost stays
        bounded. The naive slide-to-max_window policy freezes captions
        on any over longer than the window: it drops uncommitted audio
        and re-voids the hypothesis on every decode. Here alignment
        survives the slide because _ingest keys everything off
        _committed_end_s, not the window start."""
        if total - self._base <= self.max_window:
            return
        committed_smp = int(
            max(0.0, self._committed_end_s - _SLIDE_OVERLAP_S) * SAMPLE_RATE)
        hard = total - int(self.max_window * _HARD_WINDOW_FACTOR)
        new_base = max(self._base, committed_smp, hard)
        new_base = min(new_base, total - SAMPLE_RATE)   # keep >= 1 s
        if new_base > self._base:
            self._base = new_base

    def _ingest(self, words: list, base_s: float) -> tuple[str, str]:
        """Fold one decode's word list ([word, start_s, end_s] relative to
        the window) into committed/pending state. Returns the full display
        strings. Pure state-machine — tests drive this directly.

        Words lying inside already-committed audio (the committed prefix
        of an unslid window, or the seam overlap after a slide) are
        dropped by midpoint, so the comparison always runs over the
        UNCOMMITTED tail only — which is what keeps agreement alive
        across window slides and prevents committed text from ever
        duplicating."""
        if self._committed:
            words = [w for w in words
                     if base_s + (float(w[1]) + float(w[2])) / 2
                     > self._committed_end_s]
        cur_norm = [_norm(w[0]) for w in words]
        agree = _lcp(self._prev_norm, cur_norm)
        if agree > 0:
            fresh = words[:agree]
            self._committed.extend(w[0] for w in fresh)
            self._committed_end_s = base_s + float(fresh[-1][2])
            words = words[agree:]
            cur_norm = cur_norm[agree:]
        pending = [w[0] for w in words]
        self._prev_norm = cur_norm
        return " ".join(self._committed), " ".join(pending)
