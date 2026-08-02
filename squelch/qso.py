"""QSO (conversation) session tracking — a pure, clock-free state machine.

A repeater QSO is a stateful conversation with a roster of active
participants. Attribution degrades mid-QSO when every over is matched against
the entire speaker database instead of the handful of people actually talking.
This module supplies the missing context:

  * QSO boundaries — a new conversation starts after a silence gap, or on a
    directed call after a lull; it ends on a sign-off or when the gap expires.
  * The active roster — who is currently in the conversation, each with a
    join time, last-heard time and confidence, decaying out when they go quiet
    so a station that signed off stops absorbing attributions.
  * A turn-taking prior — repeater QSOs are round-robin, so the participant
    silent longest is the most likely next speaker. This is a WEIGHTING prior,
    never a hard rule (doubling, breakers and roundtables happen).
  * Roster-first voice matching — score voice candidates against the current
    roster first, falling back to the full speaker database only when the
    roster cannot answer confidently. A full-DB winner joins the roster.

The machine holds NO wall clock and touches NO database: every method takes
explicit timestamps and plain data, so tests drive it with synthetic
transmission timelines and no sleeps. Persistence (the qso / qso_participant
tables and transmissions.qso_id) is the caller's job — see pipeline.py. Keep
this module free of ingestion and matching-backend imports so a future
fingerprinter can plug in without touching QSO logic.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ---- transcript cues (self-contained; independent of the callsign DB) ----

# a directed call opening a fresh exchange: "W9XYZ this is W9ABC", or the
# calling/listening idioms hams use to solicit a contact
_DIRECTED_RE = re.compile(
    r"\bthis\s+is\b|\bcalling\b|\blistening\b|\bmonitoring\b|"
    r"\blooking\s+for\b|\bany(?:body|one)\s+(?:around|on|out|copy)\b",
    re.IGNORECASE)

# an explicit sign-off ending the exchange: "73", "clear", "final"
_SIGNOFF_RE = re.compile(
    r"\b(?:73|seventy[\s-]?three|clear|final|signing\s+(?:off|out))\b",
    re.IGNORECASE)


def is_directed_call(transcript: str | None) -> bool:
    return bool(transcript and _DIRECTED_RE.search(transcript))


def is_signoff(transcript: str | None) -> bool:
    return bool(transcript and _SIGNOFF_RE.search(transcript))


@dataclass
class QsoConfig:
    """Every QSO threshold in one place (mirrored by the [qso] TOML section
    in config.py, which is the canonical operator-facing documentation)."""
    enabled: bool = True
    gap_secs: float = 120.0          # silence longer than this starts a new QSO
    directed_idle_secs: float = 45.0  # a directed call after this idle -> new QSO
    signoff_ends: bool = True        # "73"/"clear"/"final" closes the QSO
    decay_mins: float = 15.0         # a participant silent this long drops off
    turn_prior_weight: float = 0.04  # max cosine bonus for the longest-silent member
    roster_assign_cos: float = 0.80  # relaxed voice bar for a roster member
    roster_margin: float = 0.04      # roster winner must beat the runner-up by this


@dataclass
class Participant:
    speaker_id: int | None
    callsign: str | None
    joined_at: float
    last_heard: float
    confidence: float
    tx_count: int = 0


@dataclass
class Qso:
    started_at: float
    last_activity: float
    id: int | None = None            # DB id, filled in by the persistence glue
    ended_at: float | None = None
    ended_reason: str | None = None
    participants: dict = field(default_factory=dict)   # key -> Participant
    turn_order: list = field(default_factory=list)     # participant keys, speaking order


@dataclass
class RosterMatch:
    speaker_id: int
    cosine: float                    # raw cosine (for storage/scoring)
    adjusted: float                  # cosine + turn prior (the decision score)
    margin: float                    # adjusted margin over the runner-up


def _key(speaker_id: int | None, callsign: str | None):
    """Stable roster key: prefer the speaker id, fall back to the callsign."""
    if speaker_id is not None:
        return ("spk", speaker_id)
    return ("cs", (callsign or "").upper())


class QsoTracker:
    """In-memory conversation state. Single-threaded by contract — the
    pipeline drives it from one worker, one transmission at a time."""

    def __init__(self, cfg: QsoConfig | None = None):
        self.cfg = cfg or QsoConfig()
        self.current: Qso | None = None

    # ---- boundaries ----

    def begin_tx(self, started_at: float, transcript: str | None) -> bool:
        """Register an arriving transmission and decide the QSO boundary.

        Returns True when this tx STARTS a new QSO (the caller should create a
        qso row and copy its id onto ``self.current.id``), False when it
        continues the current one. Always leaves ``self.current`` pointing at
        the QSO this tx belongs to and advances its last_activity."""
        cur = self.current
        started_new = False
        if cur is None or cur.ended_at is not None:
            started_new = True
        else:
            gap = started_at - cur.last_activity
            if gap > self.cfg.gap_secs:
                started_new = True
            elif is_directed_call(transcript) and gap > self.cfg.directed_idle_secs:
                started_new = True
        if started_new:
            self.current = Qso(started_at=started_at, last_activity=started_at)
        else:
            self.current.last_activity = started_at
        return started_new

    def note_signoff(self, transcript: str | None, at: float) -> bool:
        """Close the current QSO when the transcript is a sign-off. The next
        transmission then opens a fresh QSO. Returns True when one ended."""
        if (self.cfg.signoff_ends and self.current is not None
                and self.current.ended_at is None
                and is_signoff(transcript)):
            self.current.ended_at = at
            self.current.ended_reason = "signoff"
            return True
        return False

    # ---- roster ----

    def active_participants(self, now: float) -> list[Participant]:
        """Roster members still active — heard within the decay window."""
        if self.current is None:
            return []
        horizon = self.cfg.decay_mins * 60.0
        return [p for p in self.current.participants.values()
                if (now - p.last_heard) <= horizon]

    def record_id(self, speaker_id: int | None, callsign: str | None,
                  confidence: float, heard_at: float) -> None:
        """A confident attribution joins/updates the roster and advances the
        turn order (most-recent speaker moves to the end of the round)."""
        if self.current is None:
            return
        k = _key(speaker_id, callsign)
        p = self.current.participants.get(k)
        if p is None:
            p = Participant(speaker_id=speaker_id, callsign=callsign,
                            joined_at=heard_at, last_heard=heard_at,
                            confidence=confidence)
            self.current.participants[k] = p
        else:
            p.last_heard = heard_at
            p.confidence = max(p.confidence, confidence)
            if speaker_id is not None and p.speaker_id is None:
                p.speaker_id = speaker_id
            if callsign and not p.callsign:
                p.callsign = callsign
        p.tx_count += 1
        if k in self.current.turn_order:
            self.current.turn_order.remove(k)
        self.current.turn_order.append(k)

    def roster_prior(self, now: float) -> dict[int, float]:
        """Turn-taking prior: speaker_id -> cosine bonus, largest for the
        active member silent longest (most likely up next), ~0 for whoever
        just spoke — so we don't hand two overs in a row to one person on a
        near-tie. Pure function of the members' last_heard times."""
        members = [p for p in self.active_participants(now)
                   if p.speaker_id is not None]
        if not members:
            return {}
        members.sort(key=lambda p: p.last_heard)   # oldest silence first
        n = len(members)
        w = self.cfg.turn_prior_weight
        if n == 1:
            return {members[0].speaker_id: w}
        return {p.speaker_id: w * (1.0 - i / (n - 1))
                for i, p in enumerate(members)}

    def match_roster(self, raw_scores: dict, now: float) -> RosterMatch | None:
        """Roster-first voice match. Restricts the voice cosines
        (``raw_scores``: speaker_id -> cosine) to the active roster, applies
        the turn-taking prior, and returns a RosterMatch when a member clears
        the relaxed roster bar AND out-margins the other members. Returns None
        when the roster cannot answer — the caller then falls back to the full
        speaker database (and, if that names a stranger, adds them here)."""
        cand = {p.speaker_id: raw_scores[p.speaker_id]
                for p in self.active_participants(now)
                if p.speaker_id is not None and p.speaker_id in raw_scores}
        if not cand:
            return None
        prior = self.roster_prior(now)
        adj = {sid: cos + prior.get(sid, 0.0) for sid, cos in cand.items()}
        order = sorted(adj.items(), key=lambda kv: kv[1], reverse=True)
        best_sid, best_adj = order[0]
        second_adj = order[1][1] if len(order) > 1 else -1.0
        best_cos = cand[best_sid]
        if (best_cos >= self.cfg.roster_assign_cos
                and (best_adj - second_adj) >= self.cfg.roster_margin):
            return RosterMatch(speaker_id=best_sid, cosine=best_cos,
                               adjusted=best_adj, margin=best_adj - second_adj)
        return None
