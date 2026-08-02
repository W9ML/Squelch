"""DTMF detection for the 8 kHz USRP audio stream.

Two sources feed the same result shape:

- chan_usrp can deliver decoded key presses out-of-band as TYPE_DTMF
  frames (the Segmenter collects those onto the Transmission), and
- this module's Goertzel-style detector recovers presses from the
  audio itself for nodes that only pass the tones through.

The pipeline prefers native frames when any arrived (they can't
false-trigger) and falls back to the audio detector.

Detection runs on ~25.6 ms windows (205 samples at 8 kHz — the classic
DTMF block size) with 50 % overlap. A window registers a digit only
when a single row tone and a single column tone dominate: both above
an absolute floor, together carrying most of the window's power,
each at least ~7 dB over its in-group runner-up, and within ~10 dB of
each other (twist). A press needs two consecutive agreeing windows
(~38 ms) and must release before the same key can register again —
that plus the dominance tests is what keeps speech from talking-off
the decoder (no harmonic check needed at these thresholds; see
tests/test_dtmf.py for the speech/noise negatives).
"""

from __future__ import annotations

import time

import numpy as np

ROW_FREQS = (697.0, 770.0, 852.0, 941.0)
COL_FREQS = (1209.0, 1336.0, 1477.0, 1633.0)
DIGITS = (("1", "2", "3", "A"),
          ("4", "5", "6", "B"),
          ("7", "8", "9", "C"),
          ("*", "0", "#", "D"))

_BLOCK_8K = 205          # samples per analysis window at 8 kHz
_MIN_ON = 2              # windows a tone pair must persist to register
_OFF_RELEASE = 3         # windows without the digit to end a press
_MIN_TONE_POWER = 2e-5   # row+col mean-square power floor (full scale = 1)
_REL_TOTAL = 0.55        # row+col must carry this share of window power
_DOMINANCE = 5.0         # best-in-group over runner-up (~7 dB)
_TWIST = 10.0            # max row/col imbalance (~10 dB either way)
# hysteresis: once a press is registered, it's HELD under these relaxed
# limits. Without the on/off split, per-window measurement jitter around
# a hard threshold (a station whose twist sits right at 10 dB, or a
# fluttering signal near the power floor) chops one held key into a
# burst of 2-5 registered presses.
# the hold floor sits well below the register floor: a fluttering mobile
# signal can dip ~10 dB mid-press, and the structural checks (rel_total,
# dominance, twist) still guard the hold — silence/noise releases via
# those, not via absolute level
_HOLD_TONE_POWER = 2.5e-6
_HOLD_REL_TOTAL = 0.45
_HOLD_DOMINANCE = 3.0
_HOLD_TWIST = 16.0       # ~12 dB


class DTMFDecoder:
    """Streaming detector: feed() int16 (or [-1,1] float) samples in any
    chunking; each call returns the presses that became confirmed, as
    {"d": digit, "t": seconds-from-stream-start} dicts."""

    def __init__(self, sample_rate: int = 8000):
        self.rate = sample_rate
        self.block = max(16, round(_BLOCK_8K * sample_rate / 8000))
        self.hop = self.block // 2
        n = np.arange(self.block)
        freqs = np.array(ROW_FREQS + COL_FREQS)
        # exact-frequency DFT correlators (better than integer-bin
        # Goertzel: DTMF tones sit between bins at this window size)
        self._exp = np.exp(-2j * np.pi * np.outer(freqs, n) / sample_rate)
        self._buf = np.zeros(0, dtype=np.float64)
        self._t0 = 0.0                # stream time of _buf[0]
        # press state machine
        self._candidate: str | None = None
        self._run = 0
        self._run_t = 0.0
        self._pressed: str | None = None
        self._gap = 0

    def feed(self, samples: np.ndarray) -> list[dict]:
        x = np.asarray(samples)
        if x.dtype.kind in "iu":
            x = x.astype(np.float64) / 32768.0
        else:
            x = x.astype(np.float64)
        self._buf = np.concatenate((self._buf, x))
        events: list[dict] = []
        nwin = (len(self._buf) - self.block) // self.hop + 1
        if nwin < 1:
            return events
        wins = np.lib.stride_tricks.sliding_window_view(
            self._buf, self.block)[:nwin * self.hop:self.hop]
        p_total = np.mean(wins * wins, axis=1)
        # per-tone mean-square power: |X|^2 * 2 / N^2 (A^2/2 for a sine
        # of amplitude A at that frequency)
        p_tone = (np.abs(wins @ self._exp.T) ** 2) * (2.0 / self.block ** 2)
        for i in range(nwin):
            t = self._t0 + i * self.hop / self.rate
            d = self._classify(p_tone[i], p_total[i])
            if d is None and self._pressed is not None:
                # hysteresis: keep holding the registered key if it still
                # passes the relaxed limits
                if self._classify(p_tone[i], p_total[i],
                                  relaxed=True) == self._pressed:
                    d = self._pressed
            ev = self._step(d, t)
            if ev is not None:
                events.append(ev)
        consumed = nwin * self.hop
        self._buf = self._buf[consumed:]
        self._t0 += consumed / self.rate
        return events

    @staticmethod
    def _classify(p: np.ndarray, p_total: float,
                  relaxed: bool = False) -> str | None:
        if p_total < 1e-6:
            return None
        floor = _HOLD_TONE_POWER if relaxed else _MIN_TONE_POWER
        rel = _HOLD_REL_TOTAL if relaxed else _REL_TOTAL
        dom = _HOLD_DOMINANCE if relaxed else _DOMINANCE
        twist = _HOLD_TWIST if relaxed else _TWIST
        rows, cols = p[:4], p[4:]
        ri = int(np.argmax(rows))
        ci = int(np.argmax(cols))
        p_r, p_c = rows[ri], cols[ci]
        if p_r + p_c < floor:
            return None
        if p_r + p_c < rel * p_total:
            return None                     # tones don't dominate: speech
        r2 = max(float(np.partition(rows, -2)[-2]), 1e-12)
        c2 = max(float(np.partition(cols, -2)[-2]), 1e-12)
        if p_r < dom * r2 or p_c < dom * c2:
            return None                     # smeared spectrum, not one pair
        if not (p_c / twist <= p_r <= p_c * twist):
            return None                     # twist out of range
        return DIGITS[ri][ci]

    def _step(self, d: str | None, t: float) -> dict | None:
        """Debounce one window's classification; returns a press event
        the moment it becomes confirmed."""
        if self._pressed is not None:
            if d == self._pressed:
                self._gap = 0
            else:
                self._gap += 1
                if self._gap >= _OFF_RELEASE:
                    self._pressed = None
                    self._candidate = d
                    self._run = 1 if d else 0
                    self._run_t = t
            return None
        if d is None:
            self._candidate = None
            self._run = 0
            return None
        if d == self._candidate:
            self._run += 1
        else:
            self._candidate = d
            self._run = 1
            self._run_t = t
        if self._run >= _MIN_ON:
            self._pressed = d
            self._gap = 0
            return {"d": d, "t": round(self._run_t, 2)}
        return None


def decode_transmission(audio: np.ndarray, sample_rate: int = 8000) -> list[dict]:
    """One-shot decode of a completed transmission's audio. Returns
    [{"d": digit, "t": offset_s}, ...] in press order."""
    if audio is None or len(audio) == 0:
        return []
    return DTMFDecoder(sample_rate).feed(audio)


class LiveDTMF:
    """live_audio sink (see Segmenter): runs the streaming detector over
    the real-time frame stream and broadcasts a 'dtmf' event per press so
    the UI keypad can light up while the tone is still sounding. When the
    node delivers native TYPE_DTMF frames, those win and the audio
    detector's output is muted for the rest of the transmission (no
    double presses). Some nodes emit the native frame only at tone END —
    after the audio detector already confirmed the press — so the first
    native digit is also deduped backward against the audio path's last
    event."""

    def __init__(self, broadcaster):
        self.broadcaster = broadcaster
        self._dec: DTMFDecoder | None = None
        self._native = False
        self._last_audio: tuple[str, float] | None = None

    def send_start_soon(self) -> None:
        self._dec = DTMFDecoder()
        self._native = False
        self._last_audio = None

    def send_frame_soon(self, pcm_bytes: bytes) -> None:
        if self._native or self._dec is None:
            return
        samples = np.frombuffer(pcm_bytes, dtype="<i2")
        for ev in self._dec.feed(samples):
            self._last_audio = (ev["d"], time.monotonic())
            self._emit(ev["d"])

    def send_end_soon(self) -> None:
        self._dec = None

    def send_dtmf_soon(self, digit: str) -> None:
        first_native = not self._native
        self._native = True
        if (first_native and self._last_audio is not None
                and self._last_audio[0] == digit
                and time.monotonic() - self._last_audio[1] < 0.6):
            return          # same press, already reported from the audio
        self._emit(digit)

    def _emit(self, digit: str) -> None:
        self.broadcaster.send_soon("dtmf", {"digit": digit})
