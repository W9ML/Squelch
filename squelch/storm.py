"""Stuck-repeater ("storm") gate: keep intermod loops off the GPU.

An intermod/stuck-carrier event shows up as a machine-gun run of keyups —
measured on a real event: 74 keyups in 95 s, the repeater keyed 78% of the
time, median gap 0.1 s. A net can key the repeater just as much of the time
(a directed check-in net runs 80%+ duty), so DUTY ALONE can't separate them —
but the keyup RATE can: intermod machine-guns dozens of keyups a minute, while
a busy net keys only a handful (even a five-minute ragchew is ONE keyup, not
fifty). Acoustics don't help (webrtcvad scores intermod noise just like
speech), so the gate keys off cadence and triggers only when BOTH hold over a
rolling window:

  - duty cycle (keyed fraction of the window) >= on_duty, AND
  - keyup rate >= on_rate_per_min.

The rate condition is what spares the long-winded operator: a single 4-minute
over is ~100% duty but ~0.3 keyups/min. During a storm, transmissions are
still stored (audio, waveform, MDC) — only the ML stages (whisper + voice ID)
are skipped, so the GPU idles instead of chewing minutes of noise, and the
feed doesn't fill with garbage. EXIT tests the rate too, not just duty: the
gate stays up only while duty AND rate both stay above the lower off_
thresholds (hysteresis). Checking duty alone would latch the gate for an
entire high-duty net once its opening check-in flurry tripped it (the real
2026-09-02 failure); now the gate releases the moment the cadence drops back
to net-like, even mid-net.
"""
from __future__ import annotations

import time
from collections import deque


class StormGate:
    def __init__(self, enabled: bool = True, window_secs: float = 90.0,
                 on_duty: float = 0.45, off_duty: float = 0.20,
                 on_rate_per_min: float = 20.0, off_rate_per_min: float = 10.0):
        self.enabled = enabled
        self.window = float(window_secs)
        self.on_duty = float(on_duty)
        self.off_duty = float(off_duty)
        self.on_rate = float(on_rate_per_min)
        self.off_rate = float(off_rate_per_min)
        self.active = False
        self._keyups: deque[tuple[float, float]] = deque()

    def note_transmission(self, started_at: float, ended_at: float) -> None:
        """Record one finished keyup (call for every transmission)."""
        if ended_at < started_at:
            ended_at = started_at
        self._keyups.append((started_at, ended_at))

    def stats(self, now: float | None = None) -> tuple[float, float, float]:
        """(enter_duty, window_duty, keyups/min) over the trailing window.

        enter_duty is measured over the span actually observed (floored at
        window/3) so a storm trips ~25-30 s in instead of waiting for keyed
        time to fill 45% of the whole window. window_duty is keyed/window and
        is what the EXIT check uses — the span-floored number would inflate
        sparse traffic (one 10 s over in an otherwise idle window reads 33%)
        and latch the gate on long after a storm died."""
        now = time.time() if now is None else now
        lo = now - self.window
        while self._keyups and self._keyups[0][1] < lo:
            self._keyups.popleft()
        keyed = 0.0
        starts = 0
        first: float | None = None
        for s, e in self._keyups:
            ss = max(s, lo)
            keyed += max(0.0, min(e, now) - ss)
            if first is None or ss < first:
                first = ss
            if s >= lo:
                starts += 1
        span = self.window if first is None else min(
            self.window, max(self.window / 3.0, now - first))
        return (keyed / span, keyed / self.window,
                starts / (self.window / 60.0))

    def update(self, now: float | None = None) -> bool:
        """Re-evaluate the state; returns True when it changed. Call this on
        every new transmission AND periodically — the window keeps sliding
        after the carrier drops, so time alone can clear a storm."""
        if not self.enabled:
            changed = self.active
            self.active = False
            return changed
        enter_duty, window_duty, rate = self.stats(now)
        if self.active:
            # stay up only while BOTH stay elevated — a real storm sustains a
            # high keyup rate; a busy net's rate drops back after its opening
            # flurry even though duty stays high. Checking duty alone latched
            # the gate for a whole net once a burst tripped it.
            new = window_duty > self.off_duty and rate > self.off_rate
        else:
            new = enter_duty >= self.on_duty and rate >= self.on_rate
        changed = new != self.active
        self.active = new
        return changed
