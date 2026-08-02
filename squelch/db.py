"""SQLite storage. One connection guarded by a lock; callers in async
context should wrap calls in asyncio.to_thread (they're all short)."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

import numpy as np

from . import mdc1200
from .callsigns import (extract_callsigns, reconcile_callsigns,
                        speaker_callsign)
from .resolve import resolve

_SCHEMA = """
CREATE TABLE IF NOT EXISTS transmissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at REAL NOT NULL,
    ended_at REAL,
    duration_ms INTEGER,
    audio_path TEXT,
    transcript TEXT,
    transcript_model TEXT,
    speaker_id INTEGER REFERENCES speakers(id),
    speaker_score REAL,
    embedding BLOB,
    mdc_json TEXT,
    peaks TEXT,
    status TEXT NOT NULL DEFAULT 'processing'
);
CREATE INDEX IF NOT EXISTS idx_tx_started ON transmissions(started_at);
CREATE INDEX IF NOT EXISTS idx_tx_speaker ON transmissions(speaker_id);

CREATE TABLE IF NOT EXISTS speakers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT NOT NULL,
    is_named INTEGER NOT NULL DEFAULT 0,
    centroid BLOB,
    n_samples INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS connections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ip TEXT,
    username TEXT,               -- NULL for anonymous visitors
    user_agent TEXT,
    connected_at REAL NOT NULL,
    disconnected_at REAL         -- NULL while the socket is still open
);
CREATE INDEX IF NOT EXISTS idx_conn_connected ON connections(connected_at);

CREATE TABLE IF NOT EXISTS users (
    username TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'admin',   -- 'super' | 'admin'
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS watchlist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT,                  -- owner: alerts are private to this user
    kind TEXT NOT NULL,            -- 'callsign' | 'mdc_unit' | 'emergency' | 'speaker'
    value TEXT NOT NULL,           -- the callsign / unit id / speaker id ('' for emergency)
    label TEXT,                    -- friendly name for the alert
    webhook TEXT,                  -- optional per-rule webhook URL
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS push_subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    endpoint TEXT NOT NULL UNIQUE, -- browser push endpoint (the delivery URL)
    p256dh TEXT NOT NULL,          -- subscription public key
    auth TEXT NOT NULL,            -- subscription auth secret
    username TEXT,                 -- who subscribed (for later per-user routing)
    ua TEXT,                       -- user-agent snippet, for the manage list
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS callsigns (
    callsign TEXT PRIMARY KEY,
    data TEXT,                     -- JSON enrichment (name/QTH/class), or NULL
    status TEXT NOT NULL,          -- 'found' | 'not_found' | 'error'
    fetched_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS speaker_embeddings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    speaker_id INTEGER NOT NULL REFERENCES speakers(id),
    tx_id INTEGER,                 -- source transmission (audit trail)
    embedding BLOB NOT NULL,
    verified INTEGER NOT NULL DEFAULT 0,  -- from manual/MDC-verified assignment
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_spk_emb ON speaker_embeddings(speaker_id);

CREATE TABLE IF NOT EXISTS mdc_operators (
    unit_raw TEXT PRIMARY KEY,     -- MDC-1200 unit ID as decoded/forwarded
    speaker_id INTEGER NOT NULL REFERENCES speakers(id),
    created_at REAL NOT NULL
);

-- a QSO is one conversation: a run of transmissions sharing a roster of
-- active participants (see qso.py). transmissions.qso_id points here.
CREATE TABLE IF NOT EXISTS qso (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at REAL NOT NULL,
    last_activity REAL NOT NULL,
    ended_at REAL,
    ended_reason TEXT             -- 'signoff' (else NULL = timed out / open)
);
CREATE INDEX IF NOT EXISTS idx_qso_started ON qso(started_at);

CREATE TABLE IF NOT EXISTS qso_participant (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    qso_id INTEGER NOT NULL REFERENCES qso(id),
    speaker_id INTEGER REFERENCES speakers(id),  -- NULL = callsign-only member
    callsign TEXT,
    joined_at REAL NOT NULL,
    last_heard REAL NOT NULL,
    confidence REAL,
    tx_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_qsopart_qso ON qso_participant(qso_id);

CREATE VIRTUAL TABLE IF NOT EXISTS tx_fts USING fts5(
    transcript, content='transmissions', content_rowid='id'
);
CREATE TRIGGER IF NOT EXISTS tx_fts_ins AFTER INSERT ON transmissions BEGIN
    INSERT INTO tx_fts(rowid, transcript) VALUES (new.id, coalesce(new.transcript,''));
END;
CREATE TRIGGER IF NOT EXISTS tx_fts_upd AFTER UPDATE OF transcript ON transmissions BEGIN
    INSERT INTO tx_fts(tx_fts, rowid, transcript) VALUES ('delete', old.id, coalesce(old.transcript,''));
    INSERT INTO tx_fts(rowid, transcript) VALUES (new.id, coalesce(new.transcript,''));
END;
CREATE TRIGGER IF NOT EXISTS tx_fts_del AFTER DELETE ON transmissions BEGIN
    INSERT INTO tx_fts(tx_fts, rowid, transcript) VALUES ('delete', old.id, coalesce(old.transcript,''));
END;
"""


class Database:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._lock = threading.Lock()
        # Say Again: when on, list/get attach cross-source callsign resolution
        # (web.py sets this from cfg.sayagain_enabled)
        self.resolve_calls = False

    def _migrate(self) -> None:
        cols = {r[1] for r in self._conn.execute(
            "PRAGMA table_info(transmissions)")}
        with self._conn:
            if "peaks" not in cols:
                self._conn.execute(
                    "ALTER TABLE transmissions ADD COLUMN peaks TEXT")
            if "origin" not in cols:
                # 'local' or the connected node number the audio came from
                self._conn.execute(
                    "ALTER TABLE transmissions ADD COLUMN origin TEXT")
            if "origin_hub" not in cols:
                # which hub reported the origin (e.g. a TACS hub 610750/751/752),
                # so a leaf node's traffic can be attributed to a specific hub
                self._conn.execute(
                    "ALTER TABLE transmissions ADD COLUMN origin_hub TEXT")
            if "voter_json" not in cols:
                # voter (RTCM) RSSI samples captured during the tx
                self._conn.execute(
                    "ALTER TABLE transmissions ADD COLUMN voter_json TEXT")
            if "words" not in cols:
                # whisper word timestamps: [[word, start, end], ...]
                self._conn.execute(
                    "ALTER TABLE transmissions ADD COLUMN words TEXT")
            if "quality" not in cols:
                # signal quality metrics: {"snr":dB,"label":..,"clipping":..}
                self._conn.execute(
                    "ALTER TABLE transmissions ADD COLUMN quality TEXT")
            if "speaker_verified" not in cols:
                # assignment provenance: 'manual'|'mdc'|'callsign'|'voice'
                self._conn.execute(
                    "ALTER TABLE transmissions ADD COLUMN speaker_verified TEXT")
            if "suggest_speaker_id" not in cols:
                # sub-threshold voice match shown as a "possible: X" chip
                self._conn.execute(
                    "ALTER TABLE transmissions ADD COLUMN suggest_speaker_id INTEGER")
                self._conn.execute(
                    "ALTER TABLE transmissions ADD COLUMN suggest_score REAL")
            if "qso_id" not in cols:
                # the conversation this transmission belongs to (qso.id)
                self._conn.execute(
                    "ALTER TABLE transmissions ADD COLUMN qso_id INTEGER")
            if "dtmf_json" not in cols:
                # DTMF presses heard during the tx: [{"d":"*","t":1.2}, ...]
                self._conn.execute(
                    "ALTER TABLE transmissions ADD COLUMN dtmf_json TEXT")
            wcols = {r[1] for r in self._conn.execute(
                "PRAGMA table_info(watchlist)")}
            if "username" not in wcols:
                # alerts became per-user: give existing global watches to the
                # super admin (they were created by/for the operator) so they
                # keep firing for that account and nobody else
                self._conn.execute(
                    "ALTER TABLE watchlist ADD COLUMN username TEXT")
                sup = self._conn.execute(
                    "SELECT username FROM users WHERE role='super'"
                    " ORDER BY rowid LIMIT 1").fetchone()
                if sup:
                    self._conn.execute(
                        "UPDATE watchlist SET username=? WHERE username IS NULL",
                        (sup[0],))
            ucols = {r[1] for r in self._conn.execute(
                "PRAGMA table_info(users)")}
            if "role" not in ucols:
                self._conn.execute(
                    "ALTER TABLE users ADD COLUMN role TEXT NOT NULL "
                    "DEFAULT 'admin'")
                # existing accounts keep full rights (super) so the
                # upgrade never locks anyone out of user management
                self._conn.execute("UPDATE users SET role='super'")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---- transmissions ----

    def insert_transmission(self, started_at: float, ended_at: float,
                            duration_ms: int) -> int:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO transmissions (started_at, ended_at, duration_ms)"
                " VALUES (?,?,?)", (started_at, ended_at, duration_ms))
            return cur.lastrowid

    def last_audio_ts(self) -> float | None:
        """Wall-clock end time of the most recent transmission, or None if the
        node has never sent audio. Used to seed the live 'last frame' clock at
        startup so a restart doesn't read as 'no audio ever received'."""
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(ended_at) AS t FROM transmissions").fetchone()
        return row["t"] if row and row["t"] else None

    def update_transmission(self, tx_id: int, **fields) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._lock, self._conn:
            self._conn.execute(
                f"UPDATE transmissions SET {cols} WHERE id=?",
                (*fields.values(), tx_id))

    def get_transmission(self, tx_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                self._TX_SELECT + " WHERE t.id=?", (tx_id,)).fetchone()
        if row is None:
            return None
        d = self._tx_row_to_dict(row)
        if self.resolve_calls:
            self._attach_resolution(d)
        return d

    _TX_SELECT = ("SELECT t.id, t.started_at, t.ended_at, t.duration_ms,"
                  " t.audio_path, t.transcript, t.transcript_model,"
                  " t.speaker_id, t.speaker_score, t.speaker_verified,"
                  " t.suggest_speaker_id, t.suggest_score, t.mdc_json, t.peaks,"
                  " t.dtmf_json,"
                  " t.origin, t.origin_hub, t.voter_json, t.words, t.quality,"
                  " t.status, t.qso_id,"
                  " s.label AS speaker_label, s.is_named AS speaker_named,"
                  " sg.label AS suggest_label"
                  " FROM transmissions t LEFT JOIN speakers s ON s.id=t.speaker_id"
                  " LEFT JOIN speakers sg ON sg.id=t.suggest_speaker_id")

    @staticmethod
    def _normalize_mdc(entries: list) -> list:
        """Backfill unit_raw/type/source on audio-decoded MDC entries stored
        before MDCPacket.to_dict carried them (pre-2026-07-16 rows). The
        operator mapping, badge linking, and unit filter all key on these."""
        for m in entries:
            if isinstance(m, dict) and not m.get("unit_raw") and m.get("unit_id_hex"):
                m["unit_raw"] = m["unit_id_hex"]
                m.setdefault("source", "audio")
                t = mdc1200.OP_TYPES.get(m.get("op"))
                if t:
                    m.setdefault("type", t)
        return entries

    @staticmethod
    def _tx_row_to_dict(row: sqlite3.Row) -> dict:
        d = dict(row)
        d["has_audio"] = bool(d.pop("audio_path"))
        d["mdc"] = Database._normalize_mdc(json.loads(d.pop("mdc_json") or "[]"))
        d["peaks"] = json.loads(d["peaks"]) if d["peaks"] else None
        dj = d.pop("dtmf_json")
        d["dtmf"] = json.loads(dj) if dj else None
        vj = d.pop("voter_json")
        d["voter"] = json.loads(vj) if vj else None
        d["words"] = json.loads(d["words"]) if d["words"] else None
        d["quality"] = json.loads(d["quality"]) if d["quality"] else None
        calls = extract_callsigns(d.get("transcript") or "")
        # a mis-heard call snaps to the confirmed speaker's callsign
        if d.get("speaker_named") and d.get("speaker_label"):
            calls = reconcile_callsigns(
                calls, extract_callsigns(d["speaker_label"]))
        d["callsigns"] = calls
        return d

    def _attach_resolution(self, d: dict) -> None:
        """Say Again: annotate the over's callsigns with cross-source
        resolution. Runs OUTSIDE the connection lock (it calls
        get_cached_callsign, which takes the non-reentrant lock), so call it
        only after the row dict is built."""
        heard = extract_callsigns(d.get("transcript") or "")
        if not heard:
            d["callsigns_resolved"] = []
            return
        id_calls: set[str] = set()
        authority = 0
        if d.get("speaker_named") and d.get("speaker_label"):
            id_calls = set(extract_callsigns(d["speaker_label"]))
            # an MDC unit or a manual name is a HARD identity; a voiceprint or
            # a spoken-callsign attribution is soft
            authority = 2 if d.get("speaker_verified") in ("manual", "mdc") else 1
        sc, strength = speaker_callsign(d.get("transcript") or "")
        self_call = sc if strength == "strong" else None

        def _valid(call: str) -> bool:
            rec = self.get_cached_callsign(call)
            return bool(rec and rec.get("status") == "found")

        d["callsigns_resolved"] = resolve(
            heard, id_calls, authority, self_call, _valid)

    def list_transmissions(self, limit: int = 50, before_id: int | None = None,
                           query: str | None = None,
                           speaker_id: int | None = None,
                           origin: str | None = None,
                           mdc_unit: str | None = None,
                           since: float | None = None,
                           until: float | None = None,
                           has_mdc: bool = False,
                           unnamed_only: bool = False) -> list[dict]:
        sql = self._TX_SELECT
        args: list = []
        where = []
        if query:
            # sanitize into a simple prefix-match FTS query
            terms = [t for t in query.replace('"', " ").split() if t]
            if terms:
                fts = " ".join(f'"{t}"*' for t in terms)
                where.append("t.id IN (SELECT rowid FROM tx_fts WHERE tx_fts MATCH ?)")
                args.append(fts)
        if speaker_id is not None:
            where.append("t.speaker_id = ?")
            args.append(speaker_id)
        if origin is not None:
            where.append("t.origin = ?")
            args.append(origin)
        if mdc_unit:
            # escape LIKE metacharacters so a unit value can't inject wildcards
            esc = (mdc_unit.replace("\\", "\\\\")
                   .replace("%", "\\%").replace("_", "\\_"))
            where.append("t.mdc_json LIKE ? ESCAPE '\\'")
            args.append(f'%"unit_raw": "{esc}"%')
        if has_mdc:
            where.append("t.mdc_json IS NOT NULL AND t.mdc_json != '[]'")
        if unnamed_only:
            # transmissions attributed only to an unnamed auto-cluster
            # ("Speaker N") — the queue of voices still waiting to be named.
            # NULL-speaker rows (no voice match at all) are excluded: is_named
            # is NULL for them, so "= 0" is false.
            where.append("s.is_named = 0")
        if since is not None:
            where.append("t.started_at >= ?")
            args.append(since)
        if until is not None:
            where.append("t.started_at < ?")
            args.append(until)
        if before_id is not None:
            where.append("t.id < ?")
            args.append(before_id)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY t.id DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        dicts = [self._tx_row_to_dict(r) for r in rows]
        if self.resolve_calls:
            for d in dicts:
                self._attach_resolution(d)
        return dicts

    def distinct_origins(self) -> list[str]:
        """Sources seen (for the filter UI): 'local' plus node numbers."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT origin FROM transmissions"
                " WHERE origin IS NOT NULL AND origin != ''"
                " ORDER BY origin").fetchall()
        return [r["origin"] for r in rows]

    def distinct_mdc_units(self) -> list[str]:
        """MDC-1200 unit IDs seen across all transmissions (for the filter)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT mdc_json FROM transmissions"
                " WHERE mdc_json IS NOT NULL AND mdc_json != '[]'").fetchall()
        units: set[str] = set()
        for r in rows:
            try:
                for m in json.loads(r["mdc_json"]):
                    u = m.get("unit_raw")
                    if u:
                        units.add(str(u))
            except (json.JSONDecodeError, TypeError, AttributeError):
                pass
        return sorted(units)

    def similar_transmissions(self, tx_id: int, limit: int = 20) -> list[dict]:
        """Voiceprint kNN: transmissions whose embedding is closest (cosine)
        to this one's. Ranked, with a `similarity` field added."""
        with self._lock:
            row = self._conn.execute(
                "SELECT embedding FROM transmissions WHERE id=?",
                (tx_id,)).fetchone()
            if row is None or not row["embedding"]:
                return []
            target = np.frombuffer(row["embedding"], dtype=np.float32)
            embs = self._conn.execute(
                "SELECT id, embedding FROM transmissions"
                " WHERE embedding IS NOT NULL AND id != ?", (tx_id,)).fetchall()
        tn = float(np.linalg.norm(target)) or 1e-9
        sims = []
        for e in embs:
            v = np.frombuffer(e["embedding"], dtype=np.float32)
            denom = (float(np.linalg.norm(v)) * tn) or 1e-9
            sims.append((e["id"], float(np.dot(v, target) / denom)))
        sims.sort(key=lambda kv: kv[1], reverse=True)
        out = []
        for rid, sim in sims[:limit]:
            rec = self.get_transmission(rid)
            if rec:
                rec["similarity"] = round(sim, 3)
                out.append(rec)
        return out

    def set_origin_if_empty(self, tx_id: int, origin: str,
                            hub: str | None = None) -> bool:
        """Record where a transmission's audio came from (and which hub
        reported it); first writer wins (the source at key-up is the
        authoritative one)."""
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE transmissions SET origin=?, origin_hub=?"
                " WHERE id=? AND origin IS NULL",
                (origin, hub, tx_id))
            return cur.rowcount > 0

    def oldest_audio_paths(self, limit: int = 20) -> list[tuple[int, str]]:
        """Oldest transmissions that still have audio on disk — the
        disk-pressure purge eats these first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, audio_path FROM transmissions"
                " WHERE audio_path IS NOT NULL"
                " ORDER BY started_at ASC LIMIT ?", (limit,)).fetchall()
        return [(r["id"], r["audio_path"]) for r in rows]

    def clear_audio_path(self, tx_id: int) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE transmissions SET audio_path=NULL WHERE id=?", (tx_id,))

    def find_tx_for_time(self, t: float, pre_margin: float,
                         post_margin: float) -> int | None:
        """Most recent transmission whose time window contains t, used
        to attach a forwarded MDC event to the right transmission."""
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM transmissions"
                " WHERE started_at - ? <= ? AND ? <= coalesce(ended_at, started_at) + ?"
                " ORDER BY id DESC LIMIT 1",
                (pre_margin, t, t, post_margin)).fetchone()
        return row["id"] if row else None

    def append_mdc(self, tx_id: int, entries: list[dict]) -> None:
        """Append MDC entries to a transmission, de-duplicating by
        (source, type, unit_raw) so repeated forwards don't stack."""
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT mdc_json FROM transmissions WHERE id=?",
                (tx_id,)).fetchone()
            if row is None:
                return
            current = json.loads(row["mdc_json"] or "[]")
            # keyed on (type, unit) — deliberately ignoring source, so the
            # same burst caught by both the audio decoder and the node
            # forwarder shows one badge, not two
            def _key(e: dict):
                return (e.get("type"), e.get("unit_raw") or e.get("unit_id_hex"))
            seen = {_key(e) for e in current}
            for e in entries:
                if _key(e) not in seen:
                    current.append(e)
                    seen.add(_key(e))
            self._conn.execute(
                "UPDATE transmissions SET mdc_json=? WHERE id=?",
                (json.dumps(current), tx_id))

    def get_audio_path(self, tx_id: int) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT audio_path FROM transmissions WHERE id=?",
                (tx_id,)).fetchone()
        return row["audio_path"] if row else None

    def delete_transmission(self, tx_id: int) -> str | None:
        """Delete a transmission row; returns its audio path (for file
        cleanup) or None if the row didn't exist."""
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT audio_path FROM transmissions WHERE id=?",
                (tx_id,)).fetchone()
            if row is None:
                return None
            self._conn.execute("DELETE FROM transmissions WHERE id=?", (tx_id,))
            return row["audio_path"] or None

    def count_transmissions_between(self, start: float, end: float) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM transmissions"
                " WHERE started_at >= ? AND started_at < ?",
                (start, end)).fetchone()
        return row["n"]

    def overs_for_export(self, start: float, end: float) -> list[dict]:
        """Overs whose start falls in [start, end), oldest first, with the
        fields the Time Machine exporter needs to stitch a continuous clip:
        the raw audio_path (not exposed by the API row dict) and the
        per-word timings for burned captions."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT t.id, t.started_at, t.ended_at, t.duration_ms,"
                " t.audio_path, t.words, t.transcript,"
                " s.label AS speaker_label"
                " FROM transmissions t"
                " LEFT JOIN speakers s ON s.id=t.speaker_id"
                " WHERE t.started_at >= ? AND t.started_at < ?"
                " ORDER BY t.started_at ASC",
                (start, end)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["words"] = json.loads(d["words"]) if d["words"] else None
            out.append(d)
        return out

    def delete_transmissions_between(self, start: float,
                                     end: float) -> list[str]:
        """Delete all transmissions in [start, end); returns audio paths
        for file cleanup."""
        with self._lock, self._conn:
            rows = self._conn.execute(
                "SELECT audio_path FROM transmissions"
                " WHERE started_at >= ? AND started_at < ?"
                " AND audio_path IS NOT NULL", (start, end)).fetchall()
            self._conn.execute(
                "DELETE FROM transmissions"
                " WHERE started_at >= ? AND started_at < ?", (start, end))
        return [r["audio_path"] for r in rows]

    def expire_audio(self, older_than: float) -> list[str]:
        """Null out audio paths older than the cutoff; returns the file
        paths so the caller can delete them."""
        with self._lock, self._conn:
            rows = self._conn.execute(
                "SELECT id, audio_path FROM transmissions"
                " WHERE started_at < ? AND audio_path IS NOT NULL",
                (older_than,)).fetchall()
            self._conn.execute(
                "UPDATE transmissions SET audio_path=NULL"
                " WHERE started_at < ? AND audio_path IS NOT NULL",
                (older_than,))
        return [r["audio_path"] for r in rows]

    # ---- speakers ----

    def list_speakers(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT s.id, s.label, s.is_named, s.n_samples, s.created_at,"
                " (SELECT COUNT(*) FROM transmissions t WHERE t.speaker_id=s.id) AS tx_count"
                " FROM speakers s ORDER BY s.id").fetchall()
        return [dict(r) for r in rows]

    def load_speaker_centroids(self) -> list[tuple[int, np.ndarray, int]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, centroid, n_samples FROM speakers"
                " WHERE centroid IS NOT NULL").fetchall()
        return [(r["id"], np.frombuffer(r["centroid"], dtype=np.float32),
                 r["n_samples"]) for r in rows]

    def create_speaker(self, label: str, centroid: np.ndarray | None,
                       is_named: bool = False) -> int:
        blob = centroid.astype(np.float32).tobytes() if centroid is not None else None
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO speakers (label, is_named, centroid, n_samples, created_at)"
                " VALUES (?,?,?,?,?)",
                (label, int(is_named), blob, 1 if blob else 0, time.time()))
            return cur.lastrowid

    def update_speaker_centroid(self, speaker_id: int, centroid: np.ndarray,
                                n_samples: int) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE speakers SET centroid=?, n_samples=? WHERE id=?",
                (centroid.astype(np.float32).tobytes(), n_samples, speaker_id))

    def rebuild_voiceprint(self, speaker_id: int) -> int:
        """Rebuild a speaker's voice profile from their assigned
        transmissions, REJECTING OUTLIERS — the voices wrongly folded in
        that caused several people to share one name. The dominant voice is
        seeded from verified (manual/MDC/self-ID) samples when any exist,
        else from the whole set's mean; samples too far from it are dropped.
        Replaces the profile set, refreshes the legacy centroid, and returns
        the number of samples kept (0 leaves the print untouched)."""
        from collections import Counter
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, embedding, speaker_verified FROM transmissions"
                " WHERE speaker_id=? AND embedding IS NOT NULL"
                " ORDER BY started_at DESC",
                (speaker_id,)).fetchall()
        embs = [(r["id"], np.frombuffer(r["embedding"], dtype=np.float32),
                 r["speaker_verified"]) for r in rows]
        if not embs:
            return 0
        dim = Counter(len(e) for _, e, _ in embs).most_common(1)[0][0]
        embs = [x for x in embs if len(x[1]) == dim]
        mats = np.stack([e / (np.linalg.norm(e) or 1.0) for _, e, _ in embs])
        verified = np.array([p in ("manual", "mdc", "callsign")
                             for _, _, p in embs])
        # seed the dominant direction, then iterate keep-within-cosine
        seed = mats[verified] if verified.any() else mats
        centroid = seed.mean(axis=0)
        centroid /= np.linalg.norm(centroid) or 1.0
        keep = np.ones(len(embs), dtype=bool)
        for _ in range(4):
            sims = mats @ centroid
            keep = (sims >= self._PROFILE_KEEP_COS) | verified
            if not keep.any():
                keep = verified if verified.any() else (sims >= sims.max())
            centroid = mats[keep].mean(axis=0)
            centroid /= np.linalg.norm(centroid) or 1.0
        kept = [embs[i] for i in range(len(embs)) if keep[i]][:self._PROFILE_CAP]
        with self._lock, self._conn:
            self._conn.execute(
                "DELETE FROM speaker_embeddings WHERE speaker_id=?",
                (speaker_id,))
            for tx_id, e, prov in kept:
                self._conn.execute(
                    "INSERT INTO speaker_embeddings"
                    " (speaker_id, tx_id, embedding, verified, created_at)"
                    " VALUES (?,?,?,?,?)",
                    (speaker_id, tx_id, e.astype(np.float32).tobytes(),
                     int(prov in ("manual", "mdc", "callsign")), time.time()))
            self._refresh_centroid_locked(speaker_id)
        return len(kept)

    def merge_speakers(self, src: int, dst: int) -> None:
        """Fold speaker src into dst: reassign transmissions, combine
        centroids (weighted), delete src."""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE transmissions SET speaker_id=? WHERE speaker_id=?",
                (dst, src))
            self._conn.execute(
                "UPDATE transmissions SET suggest_speaker_id=?"
                " WHERE suggest_speaker_id=?", (dst, src))
            self._conn.execute(
                "UPDATE speaker_embeddings SET speaker_id=? WHERE speaker_id=?",
                (dst, src))
            self._conn.execute(
                "UPDATE mdc_operators SET speaker_id=? WHERE speaker_id=?",
                (dst, src))
            rows = {r["id"]: r for r in self._conn.execute(
                "SELECT id, centroid, n_samples FROM speakers WHERE id IN (?,?)",
                (src, dst))}
            s, d = rows.get(src), rows.get(dst)
            if s is not None and d is not None and s["centroid"]:
                se = np.frombuffer(s["centroid"], dtype=np.float32)
                sn = max(1, s["n_samples"])
                if d["centroid"]:
                    de = np.frombuffer(d["centroid"], dtype=np.float32)
                    dn = max(1, d["n_samples"])
                    merged = (de * dn + se * sn) / (dn + sn)
                    self._conn.execute(
                        "UPDATE speakers SET centroid=?, n_samples=? WHERE id=?",
                        (merged.astype(np.float32).tobytes(), dn + sn, dst))
                else:
                    self._conn.execute(
                        "UPDATE speakers SET centroid=?, n_samples=? WHERE id=?",
                        (s["centroid"], sn, dst))
            self._conn.execute("DELETE FROM speakers WHERE id=?", (src,))

    def rename_speaker(self, speaker_id: int, label: str) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE speakers SET label=?, is_named=1 WHERE id=?",
                (label, speaker_id))
            return cur.rowcount > 0

    def find_speaker_by_label(self, label: str) -> int | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM speakers WHERE label=? COLLATE NOCASE",
                (label,)).fetchone()
        return row["id"] if row else None

    def find_speaker_by_callsign(self, callsign: str) -> int | None:
        """A speaker whose label contains the callsign as a word
        ('W9ML Michael' matches callsign W9ML)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, label FROM speakers WHERE label LIKE ?"
                " COLLATE NOCASE", (f"%{callsign}%",)).fetchall()
        import re
        pat = re.compile(rf"(?<![A-Za-z0-9]){re.escape(callsign)}(?![A-Za-z0-9])",
                         re.IGNORECASE)
        for r in rows:
            if pat.search(r["label"]):
                return r["id"]
        return None

    def get_speaker(self, speaker_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, label, is_named, n_samples FROM speakers WHERE id=?",
                (speaker_id,)).fetchone()
        return dict(row) if row else None

    def speaker_stats(self, speaker_id: int) -> dict | None:
        """Aggregate activity for a speaker's page: counts, first/last
        heard, total airtime, and a per-hour-of-day histogram."""
        with self._lock:
            spk = self._conn.execute(
                "SELECT id, label, is_named, n_samples FROM speakers WHERE id=?",
                (speaker_id,)).fetchone()
            if spk is None:
                return None
            agg = self._conn.execute(
                "SELECT COUNT(*) n, MIN(started_at) first, MAX(started_at) last,"
                " SUM(duration_ms) air FROM transmissions WHERE speaker_id=?",
                (speaker_id,)).fetchone()
            hours = self._conn.execute(
                "SELECT CAST(strftime('%H', started_at, 'unixepoch', 'localtime')"
                " AS INTEGER) h, COUNT(*) c FROM transmissions"
                " WHERE speaker_id=? GROUP BY h", (speaker_id,)).fetchall()
        hist = [0] * 24
        for r in hours:
            if r["h"] is not None:
                hist[r["h"]] = r["c"]
        return {
            "id": spk["id"], "label": spk["label"],
            "is_named": bool(spk["is_named"]), "n_samples": spk["n_samples"],
            "tx_count": agg["n"], "first_heard": agg["first"],
            "last_heard": agg["last"],
            "airtime_ms": agg["air"] or 0, "hourly": hist,
        }

    def activity_stats(self, since: float | None = None,
                       until: float | None = None,
                       tz_offset_min: int = 0, top: int = 12) -> dict:
        """Aggregate activity for the dashboard: headline totals, an
        hour-of-day x day-of-week heatmap, a top-talkers leaderboard, a
        per-day trend, and per-node / per-hub breakdowns. Bounded to
        [since, until) when given (either may be None). Time buckets use the
        viewer's local clock (tz_offset_min = minutes east of UTC) so the
        numbers match whoever is looking, regardless of the server's tz."""
        tzmod = f"{int(tz_offset_min) * 60} seconds"
        _tc: list[str] = []
        sargs: list = []
        if since is not None:
            _tc.append("started_at >= ?"); sargs.append(since)
        if until is not None:
            _tc.append("started_at < ?"); sargs.append(until)
        tcond = (" AND " + " AND ".join(_tc)) if _tc else ""
        tcond_t = tcond.replace("started_at", "t.started_at")
        with self._lock:
            tot = self._conn.execute(
                "SELECT COUNT(*) n, COALESCE(SUM(duration_ms),0) air,"
                " COUNT(DISTINCT speaker_id) spk,"
                " COALESCE(SUM(duration_ms < 2000),0) kerch,"
                " MIN(started_at) first, MAX(started_at) last"
                " FROM transmissions WHERE 1" + tcond, sargs).fetchone()
            grid = self._conn.execute(
                "SELECT CAST(strftime('%w', started_at,'unixepoch',?) AS INTEGER) dow,"
                " CAST(strftime('%H', started_at,'unixepoch',?) AS INTEGER) hod,"
                " COUNT(*) n FROM transmissions WHERE 1" + tcond +
                " GROUP BY dow, hod", [tzmod, tzmod] + sargs).fetchall()
            talkers = self._conn.execute(
                "SELECT t.speaker_id id, s.label label, s.is_named named,"
                " COUNT(*) n, COALESCE(SUM(t.duration_ms),0) air"
                " FROM transmissions t JOIN speakers s ON s.id=t.speaker_id"
                " WHERE 1" + tcond_t +
                " GROUP BY t.speaker_id ORDER BY air DESC LIMIT ?",
                sargs + [top]).fetchall()
            days = self._conn.execute(
                "SELECT date(started_at,'unixepoch',?) day, COUNT(*) n"
                " FROM transmissions WHERE 1" + tcond +
                " GROUP BY day ORDER BY day", [tzmod] + sargs).fetchall()
            by_node = self._conn.execute(
                "SELECT origin node, COUNT(*) n, COALESCE(SUM(duration_ms),0) air"
                " FROM transmissions WHERE origin IS NOT NULL" + tcond +
                " GROUP BY origin ORDER BY air DESC LIMIT ?",
                sargs + [top]).fetchall()
            by_hub = self._conn.execute(
                "SELECT origin_hub hub, COUNT(*) n, COALESCE(SUM(duration_ms),0) air"
                " FROM transmissions WHERE origin_hub IS NOT NULL" + tcond +
                " GROUP BY origin_hub ORDER BY air DESC", sargs).fetchall()
        heat = [[0] * 24 for _ in range(7)]      # heat[day_of_week][hour], 0=Sun
        for r in grid:
            if r["dow"] is not None and r["hod"] is not None:
                heat[r["dow"]][r["hod"]] = r["n"]
        return {
            "totals": {
                "count": tot["n"], "airtime_ms": tot["air"] or 0,
                "speakers": tot["spk"] or 0, "kerchunks": tot["kerch"] or 0,
                "first": tot["first"], "last": tot["last"],
            },
            "heatmap": heat,
            "talkers": [
                {"id": t["id"], "label": t["label"],
                 "is_named": bool(t["named"]), "count": t["n"],
                 "airtime_ms": t["air"] or 0} for t in talkers],
            "trend": [{"day": d["day"], "count": d["n"]} for d in days],
            "by_node": [{"node": r["node"], "count": r["n"],
                         "airtime_ms": r["air"] or 0} for r in by_node],
            "by_hub": [{"hub": r["hub"], "count": r["n"],
                        "airtime_ms": r["air"] or 0} for r in by_hub],
        }

    # ---- callsign enrichment cache ----

    def get_cached_callsign(self, callsign: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT callsign, data, status, fetched_at FROM callsigns"
                " WHERE callsign=?", (callsign,)).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["data"] = json.loads(d["data"]) if d["data"] else None
        return d

    def cache_callsign(self, callsign: str, data: dict | None,
                       status: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO callsigns (callsign, data, status, fetched_at)"
                " VALUES (?,?,?,?) ON CONFLICT(callsign) DO UPDATE SET"
                " data=excluded.data, status=excluded.status,"
                " fetched_at=excluded.fetched_at",
                (callsign, json.dumps(data) if data else None,
                 status, time.time()))

    def callsign_images(self) -> dict[str, str]:
        """callsign -> QRZ photo URL for every cached lookup that has one.
        Drives the avatar photos in the UI (one cheap SELECT, no transcript
        scanning)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT callsign, data FROM callsigns"
                " WHERE data IS NOT NULL").fetchall()
        out: dict[str, str] = {}
        for r in rows:
            try:
                img = (json.loads(r["data"]) or {}).get("image")
            except json.JSONDecodeError:
                continue
            if img:
                out[r["callsign"]] = img
        return out

    def clear_callsign_cache(self) -> None:
        """Drop all cached enrichment (e.g. after QRZ credentials change) so
        the background worker re-fetches everything with the new source."""
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM callsigns")

    def callsign_log(self, limit: int = 400) -> list[dict]:
        """The logbook: callsigns of ACTUAL operators heard on the air —
        a named speaker's own call (counted over their transmissions) and
        explicit spoken self-IDs ("this is X"). Incidental strings the
        transcriber coins mid-sentence ("at one of" -> AT1OF) and stations
        merely mentioned/called never enter the log; they stay visible only
        as chips on the transmission card itself."""
        import re
        with self._lock:
            rows = self._conn.execute(
                "SELECT t.started_at, t.transcript, s.label AS spk_label,"
                " s.is_named AS spk_named FROM transmissions t"
                " LEFT JOIN speakers s ON s.id=t.speaker_id"
                " WHERE (t.transcript IS NOT NULL AND t.transcript != '')"
                " OR s.is_named = 1"
                " ORDER BY t.started_at").fetchall()
            cache = {c["callsign"]: c["data"] for c in self._conn.execute(
                "SELECT callsign, data FROM callsigns").fetchall()}
            speakers = self._conn.execute(
                "SELECT id, label FROM speakers").fetchall()
        agg: dict[str, dict] = {}

        def _hit(cs: str, ts: float) -> None:
            e = agg.get(cs)
            if e is None:
                agg[cs] = {"callsign": cs, "count": 1,
                           "first_heard": ts, "last_heard": ts}
            else:
                e["count"] += 1
                e["last_heard"] = ts                      # rows sorted asc

        for r in rows:
            heard: set[str] = set()
            own = (extract_callsigns(r["spk_label"])
                   if r["spk_named"] and r["spk_label"] else [])
            if own:
                heard.add(own[0])          # the attributed operator's call
            cs, strength = speaker_callsign(r["transcript"] or "")
            if cs and strength == "strong":
                if own:                    # snap a garbled self-ID onto the
                    cs = reconcile_callsigns([cs], own)[0]   # known operator
                heard.add(cs)
            for c in heard:
                _hit(c, r["started_at"])
        for cs, e in agg.items():
            raw = cache.get(cs)
            if raw:
                d = json.loads(raw)
                # 'found'/'not_found' lets the UI tell "no FCC record" (DX,
                # garbled call) apart from "not looked up yet"
                e["status"] = d.get("status")
                for k in ("name", "opclass", "city", "state"):
                    if d.get(k):
                        e[k] = d[k]
            pat = re.compile(
                rf"(?<![A-Za-z0-9]){re.escape(cs)}(?![A-Za-z0-9])", re.I)
            e["speaker_id"] = next(
                (s["id"] for s in speakers if pat.search(s["label"])), None)
        # last gate against transcriber coinage that mimics a self-ID ("this
        # is to one of..." -> TO1OF): an entry with no linked speaker whose
        # lookup came back empty-handed is a ghost, not an operator. Pending
        # (not-yet-looked-up) entries stay until enrichment judges them.
        visible = [e for e in agg.values()
                   if not (e.get("status") == "not_found"
                           and e.get("speaker_id") is None)]
        return sorted(visible,
                      key=lambda e: e["last_heard"], reverse=True)[:limit]

    # ---- watchlist ----

    def list_watchlist(self, username: str) -> list[dict]:
        """A user's own watches (alerts are private per operator)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, kind, value, label, webhook, enabled, created_at"
                " FROM watchlist WHERE username=? ORDER BY id",
                (username,)).fetchall()
        return [dict(r) for r in rows]

    def add_watch(self, username: str, kind: str, value: str, label: str = "",
                  webhook: str = "") -> int:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO watchlist"
                " (username, kind, value, label, webhook, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (username, kind, value, label, webhook, time.time()))
            return cur.lastrowid

    def delete_watch(self, watch_id: int, username: str) -> bool:
        """Delete only if the watch belongs to this user."""
        with self._lock, self._conn:
            cur = self._conn.execute(
                "DELETE FROM watchlist WHERE id=? AND username=?",
                (watch_id, username))
            return cur.rowcount > 0

    def set_watch_enabled(self, watch_id: int, enabled: bool,
                          username: str) -> bool:
        """Silence/re-arm a watch without deleting it (enabled_watches only
        matches enabled=1). Owner-scoped."""
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE watchlist SET enabled=? WHERE id=? AND username=?",
                (1 if enabled else 0, watch_id, username))
            return cur.rowcount > 0

    def enabled_watches(self) -> list[dict]:
        """Every enabled watch across all users, each tagged with its owner so
        a hit is delivered only to that user."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, username, kind, value, label, webhook FROM watchlist"
                " WHERE enabled=1").fetchall()
        return [dict(r) for r in rows]

    # ---- web push subscriptions ----

    def add_push_subscription(self, endpoint: str, p256dh: str, auth: str,
                              username: str = "", ua: str = "") -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO push_subscriptions"
                " (endpoint, p256dh, auth, username, ua, created_at)"
                " VALUES (?,?,?,?,?,?)"
                " ON CONFLICT(endpoint) DO UPDATE SET"
                " p256dh=excluded.p256dh, auth=excluded.auth,"
                " username=excluded.username, ua=excluded.ua",
                (endpoint, p256dh, auth, username, ua, time.time()))

    def list_push_subscriptions(self, username: str | None = None) -> list[dict]:
        """All push subscriptions, or just one user's (for per-user alerts)."""
        with self._lock:
            if username is None:
                rows = self._conn.execute(
                    "SELECT endpoint, p256dh, auth, username"
                    " FROM push_subscriptions").fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT endpoint, p256dh, auth, username"
                    " FROM push_subscriptions WHERE username=?",
                    (username,)).fetchall()
        return [dict(r) for r in rows]

    def delete_push_subscription(self, endpoint: str) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "DELETE FROM push_subscriptions WHERE endpoint=?", (endpoint,))
            return cur.rowcount > 0

    def next_auto_speaker_number(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM speakers").fetchone()
        return row["n"] + 1

    def get_tx_embedding(self, tx_id: int) -> np.ndarray | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT embedding FROM transmissions WHERE id=?",
                (tx_id,)).fetchone()
        if row and row["embedding"]:
            return np.frombuffer(row["embedding"], dtype=np.float32)
        return None

    def assign_speaker(self, tx_id: int, speaker_id: int,
                       score: float | None = None,
                       verified: str | None = None) -> None:
        """Assign a speaker; records provenance ('manual'|'mdc'|'callsign'|
        'voice') and clears any pending suggestion."""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE transmissions SET speaker_id=?, speaker_score=?,"
                " speaker_verified=?, suggest_speaker_id=NULL,"
                " suggest_score=NULL WHERE id=?",
                (speaker_id, score, verified, tx_id))

    def set_suggestion(self, tx_id: int, speaker_id: int,
                       score: float) -> None:
        """Record a sub-threshold voice match as a suggestion (not an
        assignment) for the 'possible: X — confirm?' UI."""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE transmissions SET suggest_speaker_id=?,"
                " suggest_score=? WHERE id=? AND speaker_id IS NULL",
                (speaker_id, score, tx_id))

    # ---- speaker voice profiles (multi-embedding) ----

    _PROFILE_CAP = 20
    # cosine floor for keeping a sample in a rebuild: samples this far from
    # the speaker's dominant voice are outliers (a different voice wrongly
    # folded in) and get dropped
    _PROFILE_KEEP_COS = 0.72

    def add_speaker_embedding(self, speaker_id: int, embedding: np.ndarray,
                              tx_id: int | None = None,
                              verified: bool = False) -> bool:
        """Add one embedding to a speaker's profile set (capped). Verified
        samples (manual/MDC assignments) may displace the oldest unverified
        sample when full; unverified samples are dropped when full.
        Refreshes the legacy centroid (mean) and n_samples for display and
        fallback matching. Returns True if stored."""
        blob = embedding.astype(np.float32).tobytes()
        with self._lock, self._conn:
            n = self._conn.execute(
                "SELECT COUNT(*) AS n FROM speaker_embeddings WHERE speaker_id=?",
                (speaker_id,)).fetchone()["n"]
            if n >= self._PROFILE_CAP:
                if not verified:
                    return False
                old = self._conn.execute(
                    "SELECT id FROM speaker_embeddings WHERE speaker_id=?"
                    " ORDER BY verified ASC, created_at ASC LIMIT 1",
                    (speaker_id,)).fetchone()
                if old is None:
                    return False
                self._conn.execute(
                    "DELETE FROM speaker_embeddings WHERE id=?", (old["id"],))
            self._conn.execute(
                "INSERT INTO speaker_embeddings"
                " (speaker_id, tx_id, embedding, verified, created_at)"
                " VALUES (?,?,?,?,?)",
                (speaker_id, tx_id, blob, int(verified), time.time()))
            self._refresh_centroid_locked(speaker_id)
        return True

    def _refresh_centroid_locked(self, speaker_id: int) -> None:
        """Recompute the legacy centroid/n_samples from the profile set.
        Caller holds the lock and an open transaction."""
        rows = self._conn.execute(
            "SELECT embedding FROM speaker_embeddings WHERE speaker_id=?",
            (speaker_id,)).fetchall()
        embs = [np.frombuffer(r["embedding"], dtype=np.float32) for r in rows]
        dims = {len(e) for e in embs}
        if embs and len(dims) == 1:
            centroid = np.mean(np.stack(embs), axis=0).astype(np.float32)
            self._conn.execute(
                "UPDATE speakers SET centroid=?, n_samples=? WHERE id=?",
                (centroid.tobytes(), len(embs), speaker_id))

    def load_speaker_profiles(self, dim: int) -> list[tuple[int, np.ndarray]]:
        """Per-speaker embedding matrices for scoring, restricted to the
        active embedder's dimension. Speakers with no profile rows fall back
        to their legacy centroid (dimension permitting)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT speaker_id, embedding FROM speaker_embeddings"
                " ORDER BY speaker_id").fetchall()
            cents = self._conn.execute(
                "SELECT id, centroid FROM speakers"
                " WHERE centroid IS NOT NULL").fetchall()
        groups: dict[int, list[np.ndarray]] = {}
        for r in rows:
            e = np.frombuffer(r["embedding"], dtype=np.float32)
            if len(e) == dim:
                groups.setdefault(r["speaker_id"], []).append(e)
        for r in cents:
            if r["id"] not in groups:
                e = np.frombuffer(r["centroid"], dtype=np.float32)
                if len(e) == dim:
                    groups[r["id"]] = [e]
        return [(spk, np.stack(embs)) for spk, embs in groups.items()]

    def clear_speaker_profile(self, speaker_id: int) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "DELETE FROM speaker_embeddings WHERE speaker_id=?",
                (speaker_id,))
            self._conn.execute(
                "UPDATE speakers SET centroid=NULL, n_samples=0 WHERE id=?",
                (speaker_id,))

    def unassigned_with_embeddings(self, limit: int = 500) -> list[tuple]:
        """Recent transmissions with a stored voice embedding but no speaker
        — candidates for retroactive re-scoring once profiles improve."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, embedding FROM transmissions"
                " WHERE speaker_id IS NULL AND embedding IS NOT NULL"
                " ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [(r["id"], r["embedding"]) for r in rows]

    def transcript_pending_ids(self, limit: int = 500) -> list[int]:
        """Transmissions whose transcription never ran (NULL transcript,
        audio still stored) — stranded by a restart during a model-load
        window. Skipped clips write '' so they never reappear here."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM transmissions WHERE transcript IS NULL"
                " AND audio_path IS NOT NULL ORDER BY id DESC LIMIT ?",
                (limit,)).fetchall()
        return [r["id"] for r in rows]

    def reset_all_voiceprints(self) -> dict:
        """Factory-reset voice identification: wipe every enrolled sample
        and centroid, clear voice-only attributions and pending suggestions,
        and drop unnamed auto-clusters nothing references anymore. Named
        speakers and evidence-backed (manual/mdc/callsign) attributions
        survive — and their stored embeddings RE-SEED the fresh profiles
        (learned-voice pollution dies, hardware/manual/spoken-ID truth does
        not: without this, every known operator starts as a stranger)."""
        with self._lock, self._conn:
            samples = self._conn.execute(
                "SELECT COUNT(*) FROM speaker_embeddings").fetchone()[0]
            self._conn.execute("DELETE FROM speaker_embeddings")
            self._conn.execute(
                "UPDATE speakers SET centroid=NULL, n_samples=0")
            cleared = self._conn.execute(
                "UPDATE transmissions SET speaker_id=NULL,"
                " speaker_score=NULL, speaker_verified=NULL"
                " WHERE speaker_id IS NOT NULL"
                " AND (speaker_verified IS NULL OR speaker_verified='voice')"
            ).rowcount
            self._conn.execute(
                "UPDATE transmissions SET suggest_speaker_id=NULL,"
                " suggest_score=NULL WHERE suggest_speaker_id IS NOT NULL")
            dropped = self._conn.execute(
                "DELETE FROM speakers WHERE is_named=0"
                " AND id NOT IN (SELECT speaker_id FROM transmissions"
                "                WHERE speaker_id IS NOT NULL)"
                " AND id NOT IN (SELECT speaker_id FROM qso_participant"
                "                WHERE speaker_id IS NOT NULL)"
                " AND id NOT IN (SELECT speaker_id FROM mdc_operators)"
            ).rowcount
            # re-seed each surviving speaker's profile from their newest
            # evidence-verified transmissions (cap 40, matching enrollment)
            seeded = self._conn.execute(
                "INSERT INTO speaker_embeddings"
                " (speaker_id, tx_id, embedding, verified, created_at)"
                " SELECT t.speaker_id, t.id, t.embedding,"
                "        t.speaker_verified IN ('manual','mdc'), ?"
                " FROM transmissions t"
                " WHERE t.speaker_id IS NOT NULL"
                "   AND t.speaker_verified IN ('manual','mdc','callsign')"
                "   AND t.embedding IS NOT NULL"
                "   AND t.id IN (SELECT t2.id FROM transmissions t2"
                "                WHERE t2.speaker_id = t.speaker_id"
                "                  AND t2.speaker_verified IN"
                "                      ('manual','mdc','callsign')"
                "                  AND t2.embedding IS NOT NULL"
                "                ORDER BY t2.id DESC LIMIT 40)",
                (time.time(),)).rowcount
        return {"samples_deleted": samples, "attributions_cleared": cleared,
                "clusters_dropped": dropped, "reseeded": seeded}

    # ---- MDC unit -> operator mapping (hardware-level ground truth) ----

    def set_mdc_operator(self, unit_raw: str, speaker_id: int) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO mdc_operators (unit_raw, speaker_id, created_at)"
                " VALUES (?,?,?) ON CONFLICT(unit_raw) DO UPDATE"
                " SET speaker_id=excluded.speaker_id",
                (str(unit_raw), speaker_id, time.time()))

    def delete_mdc_operator(self, unit_raw: str) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "DELETE FROM mdc_operators WHERE unit_raw=?", (str(unit_raw),))
            return cur.rowcount > 0

    def list_mdc_operators(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT m.unit_raw, m.speaker_id, s.label"
                " FROM mdc_operators m JOIN speakers s ON s.id=m.speaker_id"
                " ORDER BY m.unit_raw").fetchall()
        return [dict(r) for r in rows]

    def mdc_operator_for(self, units: list[str]) -> int | None:
        """Speaker mapped to any of the given MDC unit IDs (first match)."""
        for u in units:
            with self._lock:
                row = self._conn.execute(
                    "SELECT speaker_id FROM mdc_operators WHERE unit_raw=?",
                    (str(u),)).fetchone()
            if row:
                return row["speaker_id"]
        return None

    # ---- QSO sessions ----

    def create_qso(self, started_at: float) -> int:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO qso (started_at, last_activity) VALUES (?,?)",
                (started_at, started_at))
            return cur.lastrowid

    def update_qso(self, qso_id: int, **fields) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._lock, self._conn:
            self._conn.execute(f"UPDATE qso SET {cols} WHERE id=?",
                               (*fields.values(), qso_id))

    def upsert_qso_participant(self, qso_id: int, speaker_id: int | None,
                               callsign: str | None, joined_at: float,
                               last_heard: float, confidence: float) -> None:
        """Add a participant to a QSO, or refresh an existing one's last-heard,
        confidence and turn count. Matched on speaker_id when known, else on
        callsign, mirroring the in-memory roster key in qso.py."""
        with self._lock, self._conn:
            if speaker_id is not None:
                row = self._conn.execute(
                    "SELECT id FROM qso_participant WHERE qso_id=? AND speaker_id=?",
                    (qso_id, speaker_id)).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT id FROM qso_participant"
                    " WHERE qso_id=? AND speaker_id IS NULL AND callsign=?",
                    (qso_id, callsign)).fetchone()
            if row:
                self._conn.execute(
                    "UPDATE qso_participant SET last_heard=?,"
                    " confidence=MAX(COALESCE(confidence,0),?),"
                    " callsign=COALESCE(callsign,?), tx_count=tx_count+1"
                    " WHERE id=?",
                    (last_heard, confidence, callsign, row["id"]))
            else:
                self._conn.execute(
                    "INSERT INTO qso_participant (qso_id, speaker_id, callsign,"
                    " joined_at, last_heard, confidence, tx_count)"
                    " VALUES (?,?,?,?,?,?,1)",
                    (qso_id, speaker_id, callsign, joined_at, last_heard,
                     confidence))

    def _qso_row_to_dict(self, row: sqlite3.Row) -> dict:
        d = dict(row)
        parts = self._conn.execute(
            "SELECT speaker_id, callsign, joined_at, last_heard, confidence,"
            " tx_count FROM qso_participant WHERE qso_id=? ORDER BY joined_at",
            (d["id"],)).fetchall()
        d["participants"] = [dict(p) for p in parts]
        return d

    def get_qso(self, qso_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM qso WHERE id=?", (qso_id,)).fetchone()
            return self._qso_row_to_dict(row) if row else None

    def latest_open_qso(self) -> dict | None:
        """The most recent QSO not yet ended, with its roster — used to
        rehydrate the in-memory tracker after a restart so an ongoing
        conversation isn't split by the process bouncing."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM qso WHERE ended_at IS NULL"
                " ORDER BY id DESC LIMIT 1").fetchone()
            return self._qso_row_to_dict(row) if row else None

    # ---- users ----

    def count_users(self) -> int:
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) AS n FROM users").fetchone()["n"]

    def get_user_hash(self, username: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT password_hash FROM users WHERE username=?",
                (username,)).fetchone()
        return row["password_hash"] if row else None

    def get_user_role(self, username: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT role FROM users WHERE username=?",
                (username,)).fetchone()
        return row["role"] if row else None

    def count_supers(self) -> int:
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) AS n FROM users WHERE role='super'"
            ).fetchone()["n"]

    def list_users(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT username, role, created_at FROM users"
                " ORDER BY created_at").fetchall()
        return [dict(r) for r in rows]

    def create_user(self, username: str, password_hash: str,
                    role: str = "admin") -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO users (username, password_hash, role, created_at)"
                " VALUES (?,?,?,?)",
                (username, password_hash, role, time.time()))

    def set_user_role(self, username: str, role: str) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE users SET role=? WHERE username=?", (role, username))
            return cur.rowcount > 0

    def update_user_password(self, username: str, password_hash: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE users SET password_hash=? WHERE username=?",
                (password_hash, username))

    def delete_user(self, username: str) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "DELETE FROM users WHERE username=?", (username,))
            return cur.rowcount > 0

    # ---- settings ----

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO settings (key, value) VALUES (?,?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value))

    # ---- connection log ----

    def connection_open(self, ip: str | None, username: str | None,
                        user_agent: str | None, ts: float) -> int:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO connections (ip, username, user_agent,"
                " connected_at) VALUES (?,?,?,?)",
                (ip, username, (user_agent or "")[:300] or None, ts))
            return cur.lastrowid

    def connection_close(self, conn_id: int, ts: float) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE connections SET disconnected_at=?"
                " WHERE id=? AND disconnected_at IS NULL", (ts, conn_id))

    def close_open_connections(self, ts: float) -> None:
        """Mark every still-open row as closed — called at startup, since a
        row left open means the process that held that socket is gone."""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE connections SET disconnected_at=?"
                " WHERE disconnected_at IS NULL", (ts,))

    def list_connections(self, limit: int = 200,
                         before_id: int | None = None,
                         q: str | None = None,
                         status: str = "all") -> list[dict]:
        sql = ("SELECT id, ip, username, user_agent, connected_at,"
               " disconnected_at FROM connections")
        where = []
        args: list = []
        if before_id is not None:
            where.append("id < ?")
            args.append(before_id)
        if status == "active":
            where.append("disconnected_at IS NULL")
        elif status == "closed":
            where.append("disconnected_at IS NOT NULL")
        if q:
            like = f"%{q}%"
            where.append(
                "(ip LIKE ? OR username LIKE ? OR user_agent LIKE ?)")
            args += [like, like, like]
        if where:
            sql += " WHERE " + " AND ".join(where)
        # active connections first (so they're always grouped at the top and
        # never fall outside the fetch window), then most recent
        sql += " ORDER BY (disconnected_at IS NULL) DESC, id DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    def prune_connections(self, days: int) -> int:
        """Drop closed rows older than `days`. Returns the number removed."""
        if days <= 0:
            return 0
        cutoff = time.time() - days * 86400
        with self._lock, self._conn:
            cur = self._conn.execute(
                "DELETE FROM connections WHERE connected_at < ?"
                " AND disconnected_at IS NOT NULL", (cutoff,))
            return cur.rowcount

    @staticmethod
    def _ua_family(ua: str | None) -> str:
        """Coarse client family from a user-agent string — no external lookups
        (privacy), just substring matching. Order matters: Edge/Chrome/Safari
        UAs all contain each other's names."""
        if not ua:
            return "unknown"
        u = ua.lower()
        if any(b in u for b in ("bot", "spider", "crawl", "curl", "wget",
                                "python-requests", "httpx", "okhttp", "headless")):
            return "bot / script"
        mobile = any(m in u for m in ("mobile", "android", "iphone", "ipad", "ipod"))
        if "edg" in u:
            br = "Edge"
        elif "firefox" in u or "fxios" in u:
            br = "Firefox"
        elif "chrome" in u or "crios" in u or "chromium" in u:
            br = "Chrome"
        elif "safari" in u:
            br = "Safari"
        else:
            br = "Other"
        return f"{br} (mobile)" if mobile else br

    def connection_stats(self, since: float | None = None,
                         until: float | None = None,
                         tz_offset_min: int = 0, top: int = 12,
                         now: float | None = None) -> dict:
        """Analytics over the connection log: totals (unique IPs/users, total
        viewing time, peak concurrent viewers), an hour-of-day x day-of-week
        heatmap, a daily trend, top visitors by IP and by login, a session-
        length histogram, and a client breakdown. Time buckets use the viewer's
        local clock (tz_offset_min = minutes east of UTC). IP-level data — the
        API keeps it super/admin-only."""
        now = time.time() if now is None else now
        tzmod = f"{int(tz_offset_min) * 60} seconds"
        # session length; open sockets count up to `now`. now is a server float,
        # safe to inline (avoids threading it through every placeholder list).
        dur = f"(COALESCE(disconnected_at, {float(now)}) - connected_at)"
        _tc, sargs = [], []
        if since is not None:
            _tc.append("connected_at >= ?"); sargs.append(since)
        if until is not None:
            _tc.append("connected_at < ?"); sargs.append(until)
        tcond = (" AND " + " AND ".join(_tc)) if _tc else ""
        with self._lock:
            tot = self._conn.execute(
                f"SELECT COUNT(*) sessions, COUNT(DISTINCT ip) ips,"
                f" COUNT(DISTINCT username) users,"
                f" COALESCE(SUM({dur}),0) secs, COALESCE(AVG({dur}),0) avg,"
                f" COALESCE(SUM(username IS NULL),0) anon,"
                f" COALESCE(SUM(disconnected_at IS NULL),0) active,"
                f" MIN(connected_at) first, MAX(connected_at) last"
                f" FROM connections WHERE 1{tcond}", sargs).fetchone()
            grid = self._conn.execute(
                "SELECT CAST(strftime('%w', connected_at,'unixepoch',?) AS INTEGER) dow,"
                " CAST(strftime('%H', connected_at,'unixepoch',?) AS INTEGER) hod,"
                " COUNT(*) n FROM connections WHERE 1" + tcond +
                " GROUP BY dow, hod", [tzmod, tzmod] + sargs).fetchall()
            days = self._conn.execute(
                "SELECT date(connected_at,'unixepoch',?) day, COUNT(*) n"
                " FROM connections WHERE 1" + tcond +
                " GROUP BY day ORDER BY day", [tzmod] + sargs).fetchall()
            top_ips = self._conn.execute(
                f"SELECT ip, COUNT(*) sessions, COALESCE(SUM({dur}),0) secs,"
                f" MAX(connected_at) last FROM connections"
                f" WHERE ip IS NOT NULL{tcond}"
                f" GROUP BY ip ORDER BY sessions DESC, secs DESC LIMIT ?",
                sargs + [top]).fetchall()
            top_users = self._conn.execute(
                f"SELECT username, COUNT(*) sessions, COALESCE(SUM({dur}),0) secs,"
                f" MAX(connected_at) last FROM connections"
                f" WHERE username IS NOT NULL{tcond}"
                f" GROUP BY username ORDER BY sessions DESC, secs DESC LIMIT ?",
                sargs + [top]).fetchall()
            buckets = self._conn.execute(
                f"SELECT COALESCE(SUM({dur}<60),0) b0,"
                f" COALESCE(SUM({dur}>=60 AND {dur}<300),0) b1,"
                f" COALESCE(SUM({dur}>=300 AND {dur}<1800),0) b2,"
                f" COALESCE(SUM({dur}>=1800 AND {dur}<7200),0) b3,"
                f" COALESCE(SUM({dur}>=7200),0) b4"
                f" FROM connections WHERE 1{tcond}", sargs).fetchone()
            uas = self._conn.execute(
                "SELECT user_agent, COUNT(*) n FROM connections WHERE 1" + tcond +
                " GROUP BY user_agent", sargs).fetchall()
            intervals = self._conn.execute(
                "SELECT connected_at, disconnected_at FROM connections"
                " WHERE 1" + tcond, sargs).fetchall()
        # peak concurrent viewers: sweep open(+1)/close(-1) events, opens first
        events = []
        for r in intervals:
            events.append((r["connected_at"], 1))
            end = r["disconnected_at"] if r["disconnected_at"] is not None else now
            events.append((end, -1))
        events.sort(key=lambda e: (e[0], -e[1]))
        peak = cur = 0
        for _, delta in events:
            cur += delta
            if cur > peak:
                peak = cur
        heat = [[0] * 24 for _ in range(7)]
        for r in grid:
            if 0 <= r["dow"] < 7 and 0 <= r["hod"] < 24:
                heat[r["dow"]][r["hod"]] = r["n"]
        fam: dict[str, int] = {}
        for r in uas:
            k = self._ua_family(r["user_agent"])
            fam[k] = fam.get(k, 0) + r["n"]
        clients = sorted(({"name": k, "count": v} for k, v in fam.items()),
                         key=lambda x: -x["count"])
        return {
            "totals": {
                "sessions": tot["sessions"], "unique_ips": tot["ips"],
                "unique_users": tot["users"],
                "total_seconds": round(tot["secs"]),
                "avg_seconds": round(tot["avg"]),
                "anon_sessions": tot["anon"],
                "identified_sessions": tot["sessions"] - tot["anon"],
                "active_now": tot["active"], "peak_concurrent": peak,
                "first": tot["first"], "last": tot["last"],
            },
            "heatmap": heat,
            "trend": [{"day": r["day"], "count": r["n"]} for r in days],
            "top_ips": [{"ip": r["ip"], "sessions": r["sessions"],
                         "seconds": round(r["secs"]), "last": r["last"]}
                        for r in top_ips],
            "top_users": [{"username": r["username"], "sessions": r["sessions"],
                           "seconds": round(r["secs"]), "last": r["last"]}
                          for r in top_users],
            "durations": [
                {"label": "< 1 min", "count": buckets["b0"]},
                {"label": "1-5 min", "count": buckets["b1"]},
                {"label": "5-30 min", "count": buckets["b2"]},
                {"label": "30 min-2 h", "count": buckets["b3"]},
                {"label": "2 h+", "count": buckets["b4"]},
            ],
            "clients": clients,
        }
