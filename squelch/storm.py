"""Stuck-repeater ("storm") gate: keep intermod loops off the GPU.

An intermod/stuck-carrier event shows up as a machine-gun run of keyups —
measured on a real event: 74 keyups in 95 s, the repeater keyed 78% of the
time, median gap 0.1 s. Human traffic never looks like that: a busy net is
1-7% duty at ~1 keyup/min, and even a five-minute ragchew is ONE keyup, not
fifty. Acoustics can't separate the two (webrtcvad scores intermod noise just
like speech), but the CADENCE is unmistakable, so the gate triggers only when
BOTH hold over a rolling window:

  - duty cycle (keyed fraction of the window) >= on_duty, AND
  - keyup rate >= on_rate_per_min.

The rate condition is what spares the long-winded operator: a single 4-minute
over is ~100% duty but ~0.3 keyups/min. The duty condition spares a snappy
net: many short overs but mostly idle. During a storm, transmissions are
still stored (audio, waveform, MDC) — only the ML stages (whisper + voice ID)
are skipped, so the GPU idles instead of chewing minutes of noise, and the
feed doesn't fill with garbage. Exit uses a lower duty threshold (hysteresis)
so the gate can't flap at the boundary.
"""
from __future__ import annotations

import time
from collections import deque


class StormGate:
    def __init__(self, enabled: bool = True, window_secs: float = 90.0,
                 on_duty: float = 0.45, off_duty: float = 0.20,
                 on_rate_per_min: float = 12.0):
        self.enabled = enabled
        self.window = float(window_secs)
        self.on_duty = float(on_duty)
        self.off_duty = float(off_duty)
        self.on_rate = float(on_rate_per_min)
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
            new = window_duty > self.off_duty
        else:
            new = enter_duty >= self.on_duty and rate >= self.on_rate
        changed = new != self.active
        self.active = new
        return changed
