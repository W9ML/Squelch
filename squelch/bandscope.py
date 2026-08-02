"""Live bandscope — a short-time FFT of the receive audio, broadcast as
compact spectral columns for the browser waterfall.

A live_audio sink (see Segmenter / AudioTee, beside the browser PCM stream,
the DTMF detector and the captioner): while a transmission is in progress it
runs a Hann-windowed FFT over the 8 kHz PCM and pushes a `bscope` event
carrying one column of the waterfall — 128 log-magnitude bins spanning
0-4 kHz, quantized to bytes and base64'd (~172 chars, ~30/sec). The frontend
scrolls these into a canvas, themed to the active phosphor tube. Cheap: one
512-point rfft per column; nothing is computed or sent between overs.

Doing the FFT here (rather than in each browser off the /ws/audio PCM) means
the scope works without the listener having pressed play, is identical for
every viewer, and costs one computation instead of one per client.
"""

from __future__ import annotations

import base64

import numpy as np

from .events import Broadcaster

_FFT = 512            # window size; 8000/512 = 15.6 Hz per raw bin
_HOP = 266            # samples between columns -> ~30 columns/sec at 8 kHz
_BINS = 128           # display bins across 0-4 kHz (~31 Hz each)
# dB above the tracked per-column noise floor mapped onto 0..255. Auto-gaining
# to the floor (rather than a fixed window) keeps a louder or quieter channel
# looking the same — the noise floor stays dark, signals pop above it.
_SPAN_DB = 42.0
_SILENCE_DB = -45.0             # peak below this = essentially silence -> dark
_GROUP = (_FFT // 2) // _BINS   # raw bins folded into each display bin
_DARK = base64.b64encode(bytes(_BINS)).decode("ascii")   # an all-silent column


class LiveBandscope:
    """live_audio sink: FFT -> `bscope` waterfall columns."""

    def __init__(self, broadcaster: Broadcaster):
        self.broadcaster = broadcaster
        self._win = np.hanning(_FFT).astype(np.float32)
        self._buf = np.zeros(0, dtype=np.float32)
        self._active = False
        self._seq = 0

    # ---- live_audio sink interface (event-loop context) ----

    def send_start_soon(self) -> None:
        self._active = True
        self._buf = np.zeros(0, dtype=np.float32)
        self._seq = 0

    def send_frame_soon(self, pcm_bytes: bytes) -> None:
        if not self._active:
            return
        arr = np.frombuffer(pcm_bytes, dtype="<i2").astype(np.float32) / 32768.0
        self._buf = np.concatenate((self._buf, arr))
        # emit overlapping columns while we have a full window, hopping by _HOP
        while len(self._buf) >= _FFT:
            self.broadcaster.send_soon(
                "bscope", {"c": self._column(self._buf[:_FFT]), "seq": self._seq})
            self._seq += 1
            self._buf = self._buf[_HOP:]

    def send_end_soon(self) -> None:
        self._active = False
        self._buf = np.zeros(0, dtype=np.float32)

    # ---- helpers ----

    def _column(self, frame: np.ndarray) -> str:
        spec = np.abs(np.fft.rfft(frame * self._win))[: _FFT // 2]   # 256 bins, 0-4 kHz
        band = spec.reshape(_BINS, _GROUP).max(axis=1)               # -> 128 bins (peak-hold)
        db = 20.0 * np.log10(band + 1e-6)
        if db.max() < _SILENCE_DB:
            return _DARK
        # subtract a low percentile (the noise floor) so the tube auto-gains
        norm = np.clip((db - np.percentile(db, 20)) / _SPAN_DB, 0.0, 1.0)
        return base64.b64encode((norm * 255.0).astype(np.uint8).tobytes()).decode("ascii")
