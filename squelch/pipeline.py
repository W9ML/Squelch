"""Per-transmission processing pipeline.

The Segmenter (usrp.py) hands completed transmissions to
handle_transmission(); heavy work (MDC decode, Whisper, voice
embedding) runs in a single worker thread so transmissions are
processed in order without starving the event loop. Browser clients
get a tx_update event after each stage so results appear as they're
ready.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import wave
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from scipy.signal import resample_poly

from . import (callbook, dtmf, elevenlabs_stt, mdc1200, qrz, signal_quality,
               watchlist, webpush)
from .callsigns import extract_callsigns, speaker_callsign
from .config import Config, WHISPER_MODELS
from .db import Database
from .events import Broadcaster
from .mdc_ingest import MDCMatcher
from .qso import Participant, Qso, QsoConfig, QsoTracker, _key
from .source_ingest import SourceMatcher
from .speakerid import SpeakerIdentifier, voice_decision
from .voter import VoterCollector
from .transcribe import Transcriber
from .usrp import SAMPLE_RATE, Transmission

log = logging.getLogger(__name__)


def _edit_distance_1(a: str, b: str) -> bool:
    """True when a and b differ by exactly one substitution, insertion,
    or deletion."""
    if a == b:
        return False
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    if la == lb:
        return sum(1 for x, y in zip(a, b) if x != y) == 1
    if la > lb:
        a, b, la, lb = b, a, lb, la
    # a is shorter by one: try skipping one char of b
    i = 0
    while i < la and a[i] == b[i]:
        i += 1
    return a[i:] == b[i + 1:]


class Pipeline:
    def __init__(self, cfg: Config, db: Database, broadcaster: Broadcaster):
        self.cfg = cfg
        self.db = db
        self.broadcaster = broadcaster
        self.transcriber = Transcriber(
            device=cfg.whisper_device, compute_type=cfg.whisper_compute,
            language=cfg.language, download_root=cfg.data_dir / "models")
        self.speaker_id = SpeakerIdentifier(
            db, match_threshold=cfg.match_threshold,
            autocluster=cfg.autocluster,
            learn_threshold=cfg.learn_threshold,
            backend=cfg.embedder)
        self.speaker_id.model_dir = cfg.data_dir / "models"
        self._rx_probe = lambda: self.rx_active
        self._revisiting = False
        self.mdc_matcher = MDCMatcher(
            db, broadcaster, rx_active=lambda: self._rx_probe())
        self.source_matcher = SourceMatcher(
            db, broadcaster, rx_active=lambda: self._rx_probe())
        from .network import NetworkState
        self.network = NetworkState()
        from .voter_crypto import load_key
        voter_key = load_key(cfg.voter_key) if cfg.voter_key else None
        self.voter = VoterCollector(
            cfg.voter_sources, cfg.voter_min_interval, key=voter_key,
            gate_on_connect=cfg.voter_gate_on_connect,
            idle_timeout=cfg.voter_idle_timeout,
            idle_minutes=cfg.voter_idle_minutes,
            disabled=cfg.voter_polling_disabled)
        self._queue: asyncio.Queue[tuple[int, Transmission, bool]] = asyncio.Queue()
        # tx ids that couldn't be transcribed because no whisper model
        # was loaded yet (cold start / model switch) — retried once the
        # model is ready
        self._pending_transcribe: list[int] = []
        # callsigns waiting for background FCC enrichment (the logbook
        # fills itself in; nobody should have to click a callsign first)
        self._callbook_queue: asyncio.Queue[str] = asyncio.Queue()
        self.rx_active = False
        # QSO (conversation) tracking: match the active roster before the full
        # speaker DB. In-memory state, driven from this single worker one tx at
        # a time (see qso.py); None when disabled.
        self.qso: QsoTracker | None = None
        if cfg.qso_enabled:
            self.qso = QsoTracker(QsoConfig(
                enabled=True,
                gap_secs=cfg.qso_gap_secs,
                directed_idle_secs=cfg.qso_directed_idle_secs,
                signoff_ends=cfg.qso_signoff_ends,
                decay_mins=cfg.qso_decay_mins,
                turn_prior_weight=cfg.qso_turn_prior_weight,
                roster_assign_cos=cfg.qso_roster_assign_cos,
                roster_margin=cfg.qso_roster_margin))
            self._rehydrate_qso()

    def _rehydrate_qso(self) -> None:
        """Reload an in-progress QSO after a restart so a live conversation
        isn't split by the process bouncing. A conversation already past its
        silence gap is left closed — the next tx opens a fresh one."""
        row = self.db.latest_open_qso()
        if not row or time.time() - row["last_activity"] > self.cfg.qso_gap_secs:
            return
        q = Qso(started_at=row["started_at"],
                last_activity=row["last_activity"], id=row["id"])
        for p in row["participants"]:
            q.participants[_key(p["speaker_id"], p["callsign"])] = Participant(
                speaker_id=p["speaker_id"], callsign=p["callsign"],
                joined_at=p["joined_at"], last_heard=p["last_heard"],
                confidence=p["confidence"] or 0.0, tx_count=p["tx_count"])
        self.qso.current = q

    def set_rx_probe(self, probe) -> None:
        """Let the web layer combine segmenter state into the "is the
        node receiving right now" check the event matchers use."""
        self._rx_probe = probe

    # ---- model selection (runtime switchable) ----

    @property
    def whisper_model(self) -> str:
        return self.db.get_setting("whisper_model", self.cfg.whisper_model)

    def set_whisper_model(self, model: str) -> None:
        if model not in WHISPER_MODELS:
            raise ValueError(f"unknown model {model!r}")
        self.db.set_setting("whisper_model", model)
        # start downloading/loading it now (background) so it's warm by
        # the next transmission and never blocks the worker
        self.transcriber.ensure_model(model)

    # ---- segmenter callbacks ----

    async def on_rx_start(self) -> None:
        self.rx_active = True
        await self.broadcaster.send("rx", {"active": True})

    async def on_rx_discard(self) -> None:
        self.rx_active = False
        await self.broadcaster.send("rx", {"active": False})

    async def on_transmission(self, tx: Transmission) -> None:
        self.rx_active = False
        await self.broadcaster.send("rx", {"active": False})
        tx_id = await asyncio.to_thread(
            self.db.insert_transmission, tx.started_at, tx.ended_at,
            tx.duration_ms)
        # save audio + waveform peaks (and the voter slice) NOW, before the
        # (possibly backlogged) transcription queue, so the card is playable
        # immediately
        path = await asyncio.to_thread(self._save_wav, tx_id, tx)
        peaks = await asyncio.to_thread(self._waveform_peaks, tx.audio)
        voter = self.voter.slice_window(tx.started_at, tx.ended_at)
        await asyncio.to_thread(
            self.db.update_transmission, tx_id, audio_path=str(path),
            peaks=json.dumps(peaks),
            voter_json=json.dumps(voter) if voter else None)
        # attach any MDC IDs / origin events that arrived while keyed
        await self.mdc_matcher.on_tx_created(tx_id, tx.started_at, tx.ended_at)
        await self.source_matcher.on_tx_created(tx_id, tx.started_at, tx.ended_at)
        # safety net: the source key-up event is missed on ~8% of overs, which
        # drops the origin badge. Voter RSSI is captured PER NODE, so if we
        # have voter samples the over unambiguously came from that node — use
        # it to recover the origin the matcher missed.
        if voter and voter.get("nodes"):
            node = str(voter["nodes"][0].get("node") or "").strip()
            if node:
                await asyncio.to_thread(self.db.set_origin_if_empty, tx_id, node)
        record = await asyncio.to_thread(self.db.get_transmission, tx_id)
        await self.broadcaster.send("tx_new", {"tx": record})
        await self._queue.put((tx_id, tx, False))

    # ---- worker ----

    @property
    def queue_depth(self) -> int:
        return self._queue.qsize()

    _HEALTH_FAIL_LIMIT = 5    # consecutive processing failures before the
                              # process is treated as wedged (usually a GPU fault)

    def healthy(self) -> bool:
        """False after a run of consecutive processing failures — read by the
        systemd watchdog pinger so a wedged process gets restarted fresh
        rather than limping on with ML permanently broken. A GPU/CUDA fault
        makes every subsequent over fail but does not exit on its own, so
        Restart=always never fires without this."""
        return getattr(self, "_healthy", True)

    async def run_worker(self) -> None:
        self._fail_streak = 0
        self._healthy = True
        while True:
            tx_id, tx, is_reprocess = await self._queue.get()
            try:
                if is_reprocess:
                    record = await asyncio.to_thread(
                        self.db.get_transmission, tx_id)
                    mdc = record["mdc"] if record else []
                    dtmf_presses = (record.get("dtmf") or []) if record else []
                    # reprocessing an old tx must not disturb live QSO state
                    await self._ml_stages(tx_id, tx.audio, tx.duration_ms, mdc,
                                          dtmf=dtmf_presses, live=False)
                else:
                    await self._process(tx_id, tx)
            except Exception:
                log.exception("processing tx %d failed", tx_id)
                await asyncio.to_thread(
                    self.db.update_transmission, tx_id, status="error")
                await self._push_update(tx_id)
                self._fail_streak += 1
                if (self._fail_streak >= self._HEALTH_FAIL_LIMIT
                        and self._healthy):
                    self._healthy = False
                    log.critical(
                        "%d consecutive processing failures — flagging the "
                        "process unhealthy so the systemd watchdog restarts "
                        "it fresh (a GPU/CUDA fault does not exit on its own)",
                        self._fail_streak)
            else:
                # a clean over clears the streak: a real GPU fault fails EVERY
                # over, so only a sustained run (no success for a full
                # WatchdogSec window) actually trips the restart
                self._fail_streak = 0
                self._healthy = True
            finally:
                self._queue.task_done()

    async def request_reprocess(self, tx_id: int) -> None:
        """Re-run transcription and voice ID for a stored transmission
        (admin action). Raises ValueError with a user-facing message
        when it can't be done."""
        record = await asyncio.to_thread(self.db.get_transmission, tx_id)
        if record is None:
            raise ValueError("no such transmission")
        path = await asyncio.to_thread(self.db.get_audio_path, tx_id)
        if not path or not Path(path).exists():
            raise ValueError("audio no longer stored for this transmission")
        audio = await asyncio.to_thread(self._load_wav, path)
        tx = Transmission(started_at=record["started_at"],
                          ended_at=record["ended_at"] or record["started_at"],
                          audio=audio)
        # clear stale results and show it as processing in the UI
        await asyncio.to_thread(
            self.db.update_transmission, tx_id,
            transcript=None, transcript_model=None, speaker_id=None,
            speaker_score=None, speaker_verified=None,
            suggest_speaker_id=None, suggest_score=None,
            embedding=None, status="processing")
        await self._push_update(tx_id)
        await self._queue.put((tx_id, tx, True))

    async def request_second_opinion(self, tx_id: int) -> None:
        """Admin action: re-transcribe ONE stored over through ElevenLabs Scribe
        (cloud) and replace just its transcript + word timings. The local whisper
        pipeline is untouched and only this over's audio is sent -- an explicit,
        per-over choice. Raises ValueError (user-facing) on failure; the DB is
        written only on a successful cloud result, so an error leaves the
        existing transcript intact. Callsign chips + Say Again re-derive from the
        new transcript at read time; voice-ID attribution is left alone."""
        if not self.cfg.elevenlabs_enabled:
            raise ValueError("ElevenLabs reprocessing is not enabled")
        path = await asyncio.to_thread(self.db.get_audio_path, tx_id)
        if not path or not Path(path).exists():
            raise ValueError("audio no longer stored for this transmission")
        try:
            text, words = await asyncio.to_thread(
                elevenlabs_stt.transcribe_file, path,
                self.cfg.elevenlabs_model,
                self.cfg.elevenlabs_language or None)
        except elevenlabs_stt.ElevenLabsError as e:
            raise ValueError(f"ElevenLabs: {e}")
        await asyncio.to_thread(
            self.db.update_transmission, tx_id,
            transcript=text,
            words=json.dumps(words) if words else None,
            transcript_model=f"elevenlabs/{self.cfg.elevenlabs_model}",
            status="done")
        log.info("tx %d: reprocessed via ElevenLabs (%d words)",
                 tx_id, len(words))
        await self._push_update(tx_id)

    async def _process(self, tx_id: int, tx: Transmission) -> None:
        # audio + waveform peaks (+ voter slice) were already saved in
        # on_transmission so the card is playable immediately; here we do
        # the slower MDC + ML work.
        # MDC1200 decode on the raw 8 kHz samples
        mdc = []
        if self.cfg.mdc_enabled:
            mdc = await asyncio.to_thread(
                mdc1200.decode_transmission, tx.audio, SAMPLE_RATE)
            if mdc:
                await asyncio.to_thread(
                    self.db.update_transmission, tx_id,
                    mdc_json=json.dumps(mdc))
        # DTMF: prefer key presses chan_usrp decoded upstream (native
        # TYPE_DTMF frames — they can't false-trigger); fall back to the
        # Goertzel detector over the audio
        presses: list = []
        if self.cfg.dtmf_enabled:
            presses = tx.dtmf or await asyncio.to_thread(
                dtmf.decode_transmission, tx.audio, SAMPLE_RATE)
            if presses:
                await asyncio.to_thread(
                    self.db.update_transmission, tx_id,
                    dtmf_json=json.dumps(presses))
        await self._push_update(tx_id)

        await self._ml_stages(tx_id, tx.audio, tx.duration_ms, mdc,
                              dtmf=presses)

    async def _ml_stages(self, tx_id: int, audio: np.ndarray,
                         duration_ms: int, mdc: list, dtmf: list | None = None,
                         live: bool = True) -> None:
        """Transcription -> voice ID -> callsign resolution. Shared by
        first-pass processing and admin reprocessing (live=False, which skips
        QSO tracking so re-running an old tx can't corrupt the conversation
        state)."""
        # skip the ML stages entirely when there's no speech to work
        # with: an MDC burst with nothing behind it, a control-tone-only
        # DTMF over, or a kerchunk / squelch crash (whisper hallucinates
        # "Thanks for watching!" on those, and their embeddings poison
        # voice profiles)
        active_ms = await asyncio.to_thread(self._active_audio_ms, audio)
        mdc_only = (self.cfg.skip_mdc_only and bool(mdc)
                    and active_ms <= self._MDC_ONLY_MAX_ACTIVE_MS)
        dtmf_only = self._is_dtmf_only(active_ms, dtmf)
        speechless = active_ms < 350
        if mdc_only or speechless or dtmf_only:
            log.info("tx %d: %s (%d ms active), skipping transcription "
                     "and voice ID", tx_id,
                     "MDC burst only" if mdc_only
                     else "DTMF tones only" if dtmf_only else "no speech",
                     active_ms)
            # write "" (not NULL): NULL means "transcription never ran",
            # which the startup sweep treats as stranded work to redo
            await asyncio.to_thread(
                self.db.update_transmission, tx_id, transcript="")

        # signal-quality score from the raw audio (independent of speech)
        quality = await asyncio.to_thread(signal_quality.score, audio)
        if quality is not None:
            await asyncio.to_thread(
                self.db.update_transmission, tx_id,
                quality=json.dumps(quality))

        # 3. resample once for the ML stages
        audio_16k = await asyncio.to_thread(self._to_16k_float, audio)

        # 4. transcription (with word timestamps for karaoke sync)
        if (self.cfg.transcribe_enabled and self.transcriber.available
                and not mdc_only and not speechless and not dtmf_only):
            model = self.whisper_model
            prompt = await asyncio.to_thread(self._whisper_prompt)
            text, words = await asyncio.to_thread(
                self.transcriber.transcribe, audio_16k, model, prompt)
            if text is None:
                # model still loading — retry when it's ready
                self._pending_transcribe.append(tx_id)
                log.info("tx %d: whisper model not loaded yet, queued "
                         "for retry", tx_id)
            else:
                if text and self._is_prompt_echo(text, prompt):
                    # whisper parroted the vocabulary hint back at us
                    # (its go-to move on marginal audio) — that's not a
                    # transcript, and naming speakers from it would
                    # invent people
                    log.info("tx %d: discarded prompt-echo transcript %r",
                             tx_id, text[:60])
                    text, words = "", []
                await asyncio.to_thread(
                    self.db.update_transmission, tx_id,
                    transcript=text, transcript_model=model,
                    words=json.dumps(words) if words else None)
                # queue any heard callsigns for background FCC enrichment
                if text and self.cfg.callbook_enabled:
                    for cs in extract_callsigns(text):
                        self._callbook_queue.put_nowait(cs)
                await self._push_update(tx_id)

        # 5. voice embedding (speech portions only — lead/tail squelch
        # noise is common to every transmission and drags all embeddings
        # toward each other), scored against known profiles
        emb = None
        evaluation = None
        if (self.cfg.speaker_id_enabled and self.speaker_id.available
                and not mdc_only and not speechless and not dtmf_only
                and duration_ms >= self.cfg.min_embed_ms):
            speech = await asyncio.to_thread(self._speech_only, audio_16k)
            if speech is not None:
                emb = await asyncio.to_thread(self.speaker_id.embed, speech)
            if emb is not None:
                await asyncio.to_thread(
                    self.db.update_transmission, tx_id,
                    embedding=emb.tobytes())
                evaluation = await asyncio.to_thread(
                    self.speaker_id.evaluate, emb)

        # 6. identity resolution: MDC unit > spoken callsign > roster voice >
        # full-DB voice. Every path abstains rather than guesses — an Unknown
        # beats a wrong name.
        use_qso = live and self.qso is not None
        if use_qso:
            await self._qso_begin(tx_id)
        try:
            await self._resolve_identity(
                tx_id, emb, evaluation, qso=self.qso if use_qso else None)
        except Exception:
            log.exception("identity resolution failed for tx %d", tx_id)
        if use_qso:
            await self._qso_writeback(tx_id)

        await asyncio.to_thread(
            self.db.update_transmission, tx_id, status="done")
        await self._push_update(tx_id)
        await self._run_watchlist(tx_id)

    async def _run_watchlist(self, tx_id: int) -> None:
        try:
            rules = await asyncio.to_thread(self.db.enabled_watches)
            if not rules:
                return
            record = await asyncio.to_thread(self.db.get_transmission, tx_id)
            if not record:
                return
            hits = watchlist.match(record, rules)
            for h in hits:
                owner = h.get("username")
                log.info("tx %d: watch hit (%s) — %s",
                         tx_id, owner or "?", h["reason"])
                # per-user: only the operator who set the alert is notified
                await self.broadcaster.send_to_user(owner, "watch_hit", {
                    "tx": record, "label": h["label"], "reason": h["reason"],
                    "kind": h["kind"]})
                title = f"{record.get('speaker_label') or 'Squelch'}: {h['label']}"
                body = (record.get("transcript") or h["reason"])[:300]
                # web push reaches the owner's phones/desktops even with the app
                # closed; no-op when pywebpush isn't installed or they have none
                await asyncio.to_thread(
                    webpush.send_to_user, self.db, owner, title, body, "/", f"tx{tx_id}")
                if h["webhook"]:
                    await asyncio.to_thread(
                        watchlist.send_webhook, h["webhook"], title, body)
        except Exception:
            log.exception("watchlist check failed for tx %d", tx_id)

    # identity-resolution knobs (top-K cosine space): a callsign cannot
    # assign against a voice that clearly disagrees, and only clean agreeing
    # samples are learned. The cosine bands live in config (veto_cos,
    # cs_agree_cos, enroll_agree_cos, learn_cos) because they are embedding-
    # engine-scale-dependent — resemblyzer-era values baked in here silently
    # vetoed nearly every correct spoken callsign once TitaNet (much wider
    # cosine spread) went live.
    _ESTABLISHED_N = 5        # profile size where the veto starts applying

    def _voice_call(self, ev: dict | None):
        """Delegates to speakerid.voice_decision (shared with the eval
        harness so production and offline scoring never drift)."""
        return voice_decision(ev, self.cfg.assign_cos, self.cfg.assign_margin,
                              self.cfg.suggest_cos)

    def _enroll_ok(self, record: dict) -> bool:
        """Quality gates for LEARNING a voice sample from a transmission
        (manual UI assignments are exempt — the operator knows best):
        long enough to make a stable print, clean enough not to smear the
        profile, and not a roll call."""
        if (record.get("duration_ms") or 0) < 3000:
            return False
        q = record.get("quality") or {}
        if q.get("label") in ("clipping", "noisy", "flutter"):
            return False
        if len(record.get("callsigns") or []) > 3:
            return False
        return True

    async def _resolve_identity(self, tx_id: int, emb,
                                evaluation: dict | None,
                                qso: QsoTracker | None = None) -> None:
        """Decide who a transmission belongs to. Identity evidence, in
        priority order: a mapped MDC PTT-ID (hardware ground truth), an
        explicit spoken self-ID (strong callsign, voice-vetoed), a voice match
        against the active QSO roster, then the full-database voice match. A
        spoken callsign is EVIDENCE, never truth — hams routinely speak
        callsigns that aren't theirs. Below the assign bar the result is a
        suggestion chip or Unknown, not a name."""
        record = await asyncio.to_thread(self.db.get_transmission, tx_id)
        if not record:
            return
        raw = evaluation["raw"] if evaluation else {}

        # (a) MDC PTT-ID mapped to an operator: hardware-level ground truth
        units = [m.get("unit_raw") for m in (record.get("mdc") or [])
                 if m.get("type") == "I" and m.get("unit_raw")]
        owner = (await asyncio.to_thread(self.db.mdc_operator_for, units)
                 if units else None)
        if owner is not None:
            await asyncio.to_thread(
                self.db.assign_speaker, tx_id, owner, raw.get(owner), "mdc")
            if emb is not None and self._enroll_ok(record):
                await asyncio.to_thread(
                    self.speaker_id.enroll, owner, emb, tx_id, True)
            log.info("tx %d: MDC unit %s -> speaker %d", tx_id, units, owner)
            return

        # (b) spoken callsign, tiered by evidence strength. Resolve a heard
        # callsign BEFORE the voice-only match: a callsign that names a known
        # operator outranks a coin-flip voice guess of someone the speaker
        # never mentioned. This is the tx-1172 fix — "AD9EA mobile" must name
        # AD9EA even when his own voice sits mid-pack in a crowded band.
        cs = strength = None
        if self.cfg.use_callsigns and record.get("transcript"):
            cs, strength = speaker_callsign(record["transcript"])
        if cs and strength == "strong":
            if await self._assign_by_callsign(tx_id, record, cs, emb,
                                              evaluation):
                return
        elif cs and strength == "weak":
            if await self._assign_by_weak_callsign(tx_id, cs, raw):
                return

        # (c0) roster-first voice: prefer a member of the active conversation.
        # Being in the QSO is corroborating context, so the bar is relaxed and
        # the turn-taking prior breaks the mushy-band ties that otherwise flip
        # attribution to a stranger mid-QSO. Misses fall through to the full-DB
        # match below (whose winner then joins the roster via the write-back).
        if qso is not None and evaluation:
            rm = qso.match_roster(evaluation["raw"], record["started_at"])
            if rm is not None:
                await asyncio.to_thread(
                    self.db.assign_speaker, tx_id, rm.speaker_id, rm.cosine,
                    "voice")
                log.info("tx %d: roster voice -> speaker %d "
                         "(cos %.2f, adj margin %.2f)",
                         tx_id, rm.speaker_id, rm.cosine, rm.margin)
                return

        # (c) voice-only decision: cosine threshold + margin over the runner-up.
        # A heard callsign VETOES any contradicting voice pick: with
        # "KC8LUB mobile" right on the card, naming (or suggesting) W4WWF off
        # a borderline cosine is exactly the wrong-name failure abstention
        # exists to prevent. The mismatched voice falls through to (d) and
        # gets its own cluster instead.
        decision, best, cos = self._voice_call(evaluation)
        if decision and cs and await self._callsign_conflicts(best, cs):
            # a question-shaped over ("KD9NSC, are you around?") means the
            # call was ADDRESSED — the speaker is someone else, so a
            # confident voice assign may stand. Everything else: abstain.
            addressed = (record.get("transcript") or "").rstrip().endswith("?")
            if decision == "suggest" or not addressed:
                log.info("tx %d: voice %s speaker %d conflicts with heard "
                         "%s — abstaining", tx_id, decision, best, cs)
                decision = None
        if decision == "assign":
            await asyncio.to_thread(
                self.db.assign_speaker, tx_id, best, cos, "voice")
            # only a very clean, unambiguous match feeds itself back in
            if (cos >= self.cfg.learn_cos and evaluation["margin"] >= 0.08
                    and self._enroll_ok(record)):
                await asyncio.to_thread(
                    self.speaker_id.enroll, best, emb, tx_id, False)
            return
        if decision == "suggest":
            await asyncio.to_thread(self.db.set_suggestion, tx_id, best, cos)
            return

        # (d) unrecognized voice -> new auto cluster (kept separate so a
        # recurring unknown can be named later). Two guards against
        # fragmentation (the same person spawning many "Speaker N"): only
        # mint a cluster from an over good enough to seed a real profile
        # (_enroll_ok), and only when the voice doesn't already RESEMBLE a
        # known speaker. A best_cos in the grey band below suggest_cos is far
        # more likely a thin-profile regular that just missed the bar than a
        # true stranger — clustering it forks that regular's identity. Leave
        # it Unknown; the retroactive revisit reconnects it once the profile
        # fills back in.
        best_cos = evaluation["best_cos"] if evaluation else 0.0
        if (emb is not None and self.cfg.autocluster
                and self._enroll_ok(record)
                and best_cos < self.cfg.autocluster_max_cos):
            spk = await asyncio.to_thread(
                self.speaker_id.create_cluster, emb, tx_id)
            await asyncio.to_thread(
                self.db.assign_speaker, tx_id, spk, 1.0, "voice")

    async def _qso_begin(self, tx_id: int) -> None:
        """Decide the QSO boundary for this transmission (gap / directed call /
        prior sign-off), create the qso row when a new conversation starts, and
        stamp the transmission with its qso_id. Runs before attribution so the
        roster-first match can see the current conversation."""
        record = await asyncio.to_thread(self.db.get_transmission, tx_id)
        if not record:
            return
        started_new = self.qso.begin_tx(
            record["started_at"], record.get("transcript"))
        if started_new:
            qid = await asyncio.to_thread(
                self.db.create_qso, record["started_at"])
            self.qso.current.id = qid
        qid = self.qso.current.id
        ended = record["ended_at"] or record["started_at"]
        await asyncio.to_thread(self.db.update_transmission, tx_id, qso_id=qid)
        await asyncio.to_thread(self.db.update_qso, qid, last_activity=ended)

    async def _qso_writeback(self, tx_id: int) -> None:
        """After attribution, fold the result into the conversation: a
        confidently NAMED speaker (from any path — MDC, callsign, roster or
        full-DB voice) joins/refreshes the roster (a full-DB winner is the
        'third station joining' case), and a sign-off closes the QSO so the
        next over opens a fresh one. Auto-clusters (unnamed) never join."""
        if self.qso is None or self.qso.current is None:
            return
        rec = await asyncio.to_thread(self.db.get_transmission, tx_id)
        if not rec:
            return
        heard = rec["ended_at"] or rec["started_at"]
        sid = rec.get("speaker_id")
        if (sid is not None and rec.get("speaker_named")
                and rec.get("speaker_verified") in
                ("manual", "mdc", "callsign", "voice")):
            cs = (rec.get("callsigns") or [None])[0]
            conf = rec.get("speaker_score") or 1.0
            self.qso.record_id(sid, cs, conf, heard)
            await asyncio.to_thread(
                self.db.upsert_qso_participant, self.qso.current.id, sid, cs,
                heard, heard, conf)
        if self.qso.note_signoff(rec.get("transcript"), heard):
            await asyncio.to_thread(
                self.db.update_qso, self.qso.current.id, ended_at=heard,
                ended_reason="signoff")

    async def _assign_by_weak_callsign(self, tx_id: int, cs: str,
                                       raw: dict) -> bool:
        """Weak callsign evidence — a lone bare call, or the last of several
        in the 'CALLED, CALLER' convention. Returns True when it resolves the
        identity:

          * voice is consistent (owner's cosine >= cfg.cs_agree_cos, above the
            veto) -> ASSIGN. The spoken callsign breaks a voice tie, so NO
            runner-up margin is required and the bar is the low "not
            contradicting" floor, not the voice-alone suggest bar — this is
            what rescues 'AD9EA mobile' when AD9EA's own voice sits mid-pack
            (0.77-0.83) below a stranger's 0.86.
          * owner has no voice profile yet -> a confirm chip that bootstraps it.
          * voice on file clearly disagrees -> return False; the call was
            probably addressed or misheard, so let the voice branch decide.

        Weak evidence never enrolls; a later Rebuild folds the over in."""
        owner = await asyncio.to_thread(self.db.find_speaker_by_callsign, cs)
        if owner is None:
            return False
        oc = raw.get(owner)
        if oc is not None and oc >= self.cfg.cs_agree_cos:
            await asyncio.to_thread(
                self.db.assign_speaker, tx_id, owner, oc, "callsign")
            log.info("tx %d: weak %s + agreeing voice (cos %.2f) -> speaker %d",
                     tx_id, cs, oc, owner)
            return True
        if oc is None:
            await asyncio.to_thread(self.db.set_suggestion, tx_id, owner, 0.0)
            log.info("tx %d: weak %s, no voice on file -> suggest speaker %d",
                     tx_id, cs, owner)
            return True
        return False   # voice disagrees -> let the voice branch decide

    async def _assign_by_callsign(self, tx_id: int, record: dict, cs: str,
                                  emb, evaluation: dict | None) -> bool:
        """Handle a STRONG (explicit self-ID) callsign. Returns True when
        it produced an assignment; False lets the voice decision run."""
        raw = evaluation["raw"] if evaluation else {}
        owner = await asyncio.to_thread(self.db.find_speaker_by_callsign, cs)
        fuzzy = False
        if owner is None:
            # single-character mishears are chronic on FM (KB9NSC for
            # KD9NSC); if exactly one known callsign is 1 edit away,
            # that's who it is — but never LEARN from a fuzzy match
            owner = await asyncio.to_thread(self._fuzzy_owner, cs)
            fuzzy = owner is not None
            if fuzzy:
                spk = await asyncio.to_thread(self.db.get_speaker, owner)
                if spk and spk["label"] == cs[1:]:
                    # the known label is the heard call minus its FIRST
                    # character — the signature of whisper clipping a leading
                    # phonetic ("[Whis]key Alpha 4..." -> A4CDM). The longer
                    # heard call is the complete one: upgrade the truncated
                    # speaker instead of assigning a good read down to it.
                    await asyncio.to_thread(
                        self.db.rename_speaker, owner, cs)
                    fuzzy = False        # exact now — enrollment allowed
                    log.info("tx %d: upgraded truncated callsign %s -> %s",
                             tx_id, spk["label"], cs)

        if owner is not None:
            cos = raw.get(owner)
            spk = await asyncio.to_thread(self.db.get_speaker, owner)
            established = bool(spk and spk["n_samples"] >= self._ESTABLISHED_N)
            if established and cos is not None and cos < self.cfg.veto_cos:
                # voice veto: this voice clearly isn't the callsign's owner
                # (net-control echo, whisper mishear) — don't assign, and
                # definitely don't poison the profile
                log.info("tx %d: heard %s but voice disagrees (cos %.2f) — "
                         "not assigning", tx_id, cs, cos)
                return False
            await asyncio.to_thread(
                self.db.assign_speaker, tx_id, owner, cos, "callsign")
            if (not fuzzy and emb is not None and self._enroll_ok(record)
                    and (not established or (cos or 0) >= self.cfg.enroll_agree_cos)):
                await asyncio.to_thread(
                    self.speaker_id.enroll, owner, emb, tx_id, False)
            log.info("tx %d: self-ID %s -> speaker %d%s", tx_id, cs, owner,
                     " (fuzzy)" if fuzzy else "")
            return True

        # unknown callsign with a strong self-ID: name the voice's unnamed
        # cluster if it plausibly matches, otherwise create a new speaker
        if evaluation and evaluation["best_id"] is not None \
                and evaluation["best_cos"] >= self.cfg.suggest_cos:
            spk = await asyncio.to_thread(
                self.db.get_speaker, evaluation["best_id"])
            if spk and not spk["is_named"]:
                await asyncio.to_thread(
                    self.db.rename_speaker, evaluation["best_id"], cs)
                await asyncio.to_thread(
                    self.db.assign_speaker, tx_id, evaluation["best_id"],
                    evaluation["best_cos"], "callsign")
                await self.broadcaster.send(
                    "speaker_renamed",
                    {"speaker_id": evaluation["best_id"], "label": cs})
                if emb is not None and self._enroll_ok(record):
                    await asyncio.to_thread(
                        self.speaker_id.enroll, evaluation["best_id"], emb,
                        tx_id, False)
                log.info("tx %d: named cluster %d as %s from self-ID",
                         tx_id, evaluation["best_id"], cs)
                # the cluster just got an identity: sweep recent unknown
                # cards whose voice now resolves against it
                self.revisit_soon()
                return True
        spk_id = await asyncio.to_thread(self.db.create_speaker, cs, emb, True)
        if emb is not None:
            await asyncio.to_thread(
                self.db.add_speaker_embedding, spk_id, emb, tx_id, False)
        await asyncio.to_thread(
            self.db.assign_speaker, tx_id, spk_id, None, "callsign")
        log.info("tx %d: new speaker %s from self-ID", tx_id, cs)
        return True

    async def _push_update(self, tx_id: int) -> None:
        record = await asyncio.to_thread(self.db.get_transmission, tx_id)
        if record:
            await self.broadcaster.send("tx_update", {"tx": record})

    # ---- helpers ----

    def _save_wav(self, tx_id: int, tx: Transmission) -> Path:
        day = datetime.fromtimestamp(tx.started_at, tz=timezone.utc)
        directory = self.cfg.audio_dir / day.strftime("%Y/%m/%d")
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"tx_{tx_id}.wav"
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SAMPLE_RATE)
            w.writeframes(tx.audio.astype("<i2").tobytes())
        return path

    async def revisit_unassigned(self, limit: int = 500) -> int:
        """Re-score recent unassigned transmissions against the CURRENT
        voice profiles. Identities improve after the fact — a cluster gets
        named, a profile gains verified samples — and the cards that were
        borderline at the time should catch up instead of staying 'unknown
        voice' forever. Pure cosine math over stored embeddings (no GPU),
        same decision gates as live traffic, including the callsign veto."""
        if self._revisiting:
            return 0
        self._revisiting = True
        try:
            rows = await asyncio.to_thread(
                self.db.unassigned_with_embeddings, limit)
            changed = 0
            # Load the profile set ONCE. It's constant across the sweep —
            # assign_speaker/set_suggestion only UPDATE the tx row, never
            # enroll — so scoring every row against it is bit-identical to
            # re-loading per row (which was up to 500 full reloads/sweep).
            profiles = None
            for tx_id, blob in rows:
                emb = np.frombuffer(blob, dtype=np.float32)
                if profiles is None:
                    profiles = await asyncio.to_thread(
                        self.db.load_speaker_profiles, len(emb))
                ev = await asyncio.to_thread(
                    self.speaker_id.evaluate_against, emb, profiles)
                decision, best, cos = self._voice_call(ev)
                if not decision:
                    continue
                rec = await asyncio.to_thread(self.db.get_transmission, tx_id)
                if not rec or rec.get("speaker_id") is not None:
                    continue
                cs = None
                if self.cfg.use_callsigns and rec.get("transcript"):
                    cs, _ = speaker_callsign(rec["transcript"])
                if cs and await self._callsign_conflicts(best, cs):
                    continue
                if decision == "assign":
                    await asyncio.to_thread(
                        self.db.assign_speaker, tx_id, best, cos, "voice")
                elif rec.get("suggest_speaker_id") != best:
                    await asyncio.to_thread(
                        self.db.set_suggestion, tx_id, best, cos)
                else:
                    continue
                await self._push_update(tx_id)
                changed += 1
            if changed:
                log.info("revisit: %d older transmissions re-identified",
                         changed)
            return changed
        finally:
            self._revisiting = False

    def revisit_soon(self) -> None:
        """Fire-and-forget revisit — call after anything that improves a
        profile (cluster named, admin rename/confirm, MDC link)."""
        asyncio.ensure_future(self.revisit_unassigned())

    async def _callsign_conflicts(self, speaker_id: int, cs: str) -> bool:
        """True when a voice-picked speaker has a callsign label that
        CONTRADICTS the callsign heard in this transmission (edit-distance-1
        counts as the same operator — FM mishears are chronic). Unnamed
        clusters never conflict: they could well BE the heard callsign."""
        spk = await asyncio.to_thread(self.db.get_speaker, speaker_id)
        theirs = extract_callsigns(spk["label"]) if spk else []
        if not theirs:
            return False
        return cs not in theirs and not any(
            _edit_distance_1(cs, t) for t in theirs)

    def _fuzzy_owner(self, callsign: str) -> int | None:
        """Speaker whose callsign is exactly one edit from `callsign`.
        Returns a match only when it's unambiguous (single candidate)."""
        candidates = []
        for spk in self.db.list_speakers():
            for known in extract_callsigns(spk["label"]):
                if _edit_distance_1(callsign, known):
                    candidates.append(spk["id"])
                    break
        return candidates[0] if len(candidates) == 1 else None

    @staticmethod
    def _is_prompt_echo(text: str, prompt: str | None) -> bool:
        """True when a transcript is mostly a regurgitation of the
        vocabulary prompt rather than actual speech."""
        if not prompt:
            return False
        # dedupe transcript words first, so a looping hallucination
        # ("N9PRK, N9PRK, ...") can't dilute the prompt-overlap ratio
        twords = set(w for w in re.findall(r"[A-Za-z0-9']+", text.lower())
                     if len(w) > 2)
        if len(twords) < 4:
            return False
        pwords = set(w for w in re.findall(r"[A-Za-z0-9']+", prompt.lower())
                     if len(w) > 2)
        overlap = sum(1 for w in twords if w in pwords)
        return overlap / len(twords) >= 0.6

    def _whisper_prompt(self) -> str | None:
        """Vocabulary hint for whisper: the configured prompt plus the
        callsigns of known speakers, so local calls get spelled right
        instead of "KV9RJ" / "9-0-RJ" mangling."""
        parts = []
        if self.cfg.initial_prompt:
            parts.append(self.cfg.initial_prompt)
        if self.cfg.auto_prompt_callsigns:
            # only a few, and woven into a sentence rather than a bare
            # list, to avoid priming whisper to emit callsign lists
            calls: list[str] = []
            for spk in self.db.list_speakers():
                if not spk["is_named"]:
                    continue
                for c in extract_callsigns(spk["label"]):
                    if c not in calls:
                        calls.append(c)
            if calls:
                parts.append("Stations on frequency include "
                             + " and ".join(calls[:6]) + ".")
        return " ".join(parts) or None

    @staticmethod
    def _speech_only(audio_16k: np.ndarray,
                     win_ms: int = 50) -> np.ndarray | None:
        """Keep only the energetic windows of a 16 kHz float recording
        (for voice embeddings). Returns None if less than half a second
        of speech-like audio remains."""
        win = int(16000 * win_ms / 1000)
        n = len(audio_16k) // win
        if n == 0:
            return None
        f = audio_16k[:n * win].reshape(n, win)
        rms = np.sqrt((f ** 2).mean(axis=1))
        peak = float(rms.max())
        if peak < 0.004:
            return None
        keep = rms > max(0.004, peak * 0.15)
        if keep.sum() * win_ms < 500:
            return None
        return f[keep].reshape(-1)

    # an MDC-1200 burst is ~180 ms (double ~280 ms); anything with more
    # active audio than this has speech (or something else) in it
    _MDC_ONLY_MAX_ACTIVE_MS = 700

    @classmethod
    def _is_mdc_only(cls, audio: np.ndarray) -> bool:
        return cls._active_audio_ms(audio) <= cls._MDC_ONLY_MAX_ACTIVE_MS

    @staticmethod
    def _is_dtmf_only(active_ms: int, presses: list | None) -> bool:
        """A key press is ~100-250 ms of pure tone; when the presses
        account for essentially all the active audio there's no speech
        worth transcribing (whisper turns tone runs into "BABABABA"-style
        garbage, and tone embeddings would poison voice profiles)."""
        return bool(presses) and active_ms <= len(presses) * 250 + 300

    @staticmethod
    def _active_audio_ms(audio: np.ndarray, win_ms: int = 50) -> int:
        """Rough voice-activity measure: total duration of 50 ms windows
        with meaningful energy (relative to the loudest window, with an
        absolute floor so pure noise doesn't count)."""
        if len(audio) == 0:
            return 0
        win = int(SAMPLE_RATE * win_ms / 1000)
        n = len(audio) // win
        if n == 0:
            return 0
        f = audio[:n * win].astype(np.float32).reshape(n, win)
        rms = np.sqrt((f ** 2).mean(axis=1))
        peak = float(rms.max())
        if peak < 150:                     # ~-46 dBFS: effectively silence
            return 0
        thr = max(150.0, peak * 0.15)
        return int((rms > thr).sum() * win_ms)

    @staticmethod
    def _waveform_peaks(audio: np.ndarray, buckets: int = 140) -> list[float]:
        """Downsample |audio| to normalized per-bucket peaks for the
        UI's waveform display."""
        if len(audio) == 0:
            return []
        parts = np.array_split(np.abs(audio.astype(np.int32)), buckets)
        p = np.array([part.max() if len(part) else 0 for part in parts],
                     dtype=np.float64)
        top = p.max()
        if top <= 0:
            return [0.0] * len(p)
        return [round(float(x), 3) for x in p / top]

    @staticmethod
    def _to_16k_float(audio_8k: np.ndarray) -> np.ndarray:
        f = audio_8k.astype(np.float32) / 32768.0
        return resample_poly(f, 2, 1).astype(np.float32)

    # ---- transcription retry (model wasn't loaded yet) ----

    async def run_transcribe_retry(self) -> None:
        # the retry queue is in-memory and dies with the process: sweep the
        # DB at startup for transmissions stranded mid-load-window (NULL
        # transcript, audio still stored) so an outage or restart can never
        # permanently orphan their transcripts
        stranded = await asyncio.to_thread(self.db.transcript_pending_ids)
        if stranded:
            log.info("startup: %d transmissions awaiting transcription, "
                     "requeued", len(stranded))
            self._pending_transcribe.extend(stranded)
        while True:
            await asyncio.sleep(5)
            if not self._pending_transcribe:
                continue
            if self.transcriber.loaded_model is None:
                continue
            pending, self._pending_transcribe = self._pending_transcribe, []
            for tx_id in pending:
                try:
                    done = await self._retry_transcribe(tx_id)
                    if not done:
                        self._pending_transcribe.append(tx_id)
                except Exception:
                    log.exception("transcription retry failed for tx %d",
                                  tx_id)

    async def _retry_transcribe(self, tx_id: int) -> bool:
        """Requeue a stranded transmission through the full reprocess path.
        A text-only retry would fill the transcript but never re-run
        identity resolution — the spoken callsign in it would stay
        unassigned forever."""
        try:
            await self.request_reprocess(tx_id)
        except ValueError:
            return True                     # audio gone; nothing to do
        log.info("tx %d: requeued for full reprocess (stranded transcript)",
                 tx_id)
        return True

    @staticmethod
    def _load_wav(path: str) -> np.ndarray:
        with wave.open(path, "rb") as w:
            return np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")

    # ---- background callsign enrichment (the logbook fills itself in) ----

    _CALLBOOK_SPACING = 2.0   # seconds between live FCC lookups (be kind)

    def lookup_callsign(self, cs: str) -> dict:
        """One enrichment lookup: QRZ XML (email + photo + DX coverage) when
        the admin has configured QRZ credentials, falling back to the free
        FCC feed on any QRZ failure or when unconfigured."""
        user = self.db.get_setting("qrz_username", "")
        pw = self.db.get_setting("qrz_password", "")
        if user and pw:
            result = qrz.lookup(user, pw, cs)
            if result["status"] in ("found", "not_found"):
                return result
        return callbook.lookup(cs)

    def requeue_callsigns(self) -> int:
        """Queue every logbook callsign for (re-)enrichment — used after the
        QRZ credentials change so existing entries pick up email/photo."""
        n = 0
        for row in self.db.callsign_log():
            self._callbook_queue.put_nowait(row["callsign"])
            n += 1
        return n

    def _enrich_callsign(self, cs: str) -> bool:
        """Look up and cache one callsign unless it's already cached fresh.
        Returns True only when a network lookup was actually performed
        (drives the worker's rate limiting)."""
        if not self.cfg.callbook_enabled:
            return False
        cached = self.db.get_cached_callsign(cs)
        ttl = self.cfg.callbook_cache_days * 86400
        if cached and (self.cfg.callbook_cache_days <= 0
                       or time.time() - cached["fetched_at"] < ttl):
            return False
        result = self.lookup_callsign(cs)
        if result["status"] in ("found", "not_found"):    # don't cache errors
            self.db.cache_callsign(cs, result, result["status"])
        return True

    async def run_callbook_worker(self) -> None:
        """Enrich heard callsigns automatically so the logbook never shows
        'not looked up yet' for long. Seeds itself with every already-heard,
        never-cached callsign, then follows new transcripts live. Lookups
        are spaced out to stay polite to callook.info."""
        if not self.cfg.callbook_enabled:
            return
        try:
            for row in await asyncio.to_thread(self.db.callsign_log):
                if await asyncio.to_thread(
                        self.db.get_cached_callsign, row["callsign"]) is None:
                    self._callbook_queue.put_nowait(row["callsign"])
        except Exception:
            log.exception("callbook backfill seed failed")
        while True:
            cs = await self._callbook_queue.get()
            try:
                did = await asyncio.to_thread(self._enrich_callsign, cs)
            except Exception:
                log.exception("callbook enrichment failed for %s", cs)
                did = False
            finally:
                self._callbook_queue.task_done()
            if did:
                await asyncio.sleep(self._CALLBOOK_SPACING)

    # ---- retention / disk maintenance ----

    async def run_retention(self) -> None:
        """Periodic maintenance: age-based retention plus a disk-space
        guard that purges the oldest audio before the disk fills up and
        takes the whole service down."""
        while True:
            try:
                if self.cfg.retention_days > 0:
                    cutoff = time.time() - self.cfg.retention_days * 86400
                    paths = await asyncio.to_thread(self.db.expire_audio, cutoff)
                    self._unlink_all(paths)
                    if paths:
                        log.info("retention: removed %d old recordings",
                                 len(paths))
                await asyncio.to_thread(self._disk_guard)
            except Exception:
                log.exception("maintenance pass failed")
            await asyncio.sleep(600)

    def _disk_guard(self) -> None:
        """Free space below min_free_gb -> delete oldest recordings
        (audio only; transcripts and metadata are kept) until we're
        comfortably above the line again."""
        import shutil as _shutil
        min_free = self.cfg.min_free_gb * 1e9
        if min_free <= 0:
            return
        target = min_free * 1.25          # purge past the line a bit
        freed = 0
        while _shutil.disk_usage(self.cfg.data_dir).free < min_free:
            batch = self.db.oldest_audio_paths(limit=25)
            if not batch:
                log.error("disk guard: below %.1f GB free but no audio "
                          "left to purge!", self.cfg.min_free_gb)
                return
            for tx_id, path in batch:
                try:
                    p = Path(path)
                    size = p.stat().st_size if p.exists() else 0
                    p.unlink(missing_ok=True)
                    freed += size
                except OSError as e:
                    log.warning("disk guard: could not delete %s: %s", path, e)
                self.db.clear_audio_path(tx_id)
                if _shutil.disk_usage(self.cfg.data_dir).free >= target:
                    break
        if freed:
            log.warning("disk guard: low disk space — purged %.1f MB of "
                        "oldest recordings", freed / 1e6)
            self.broadcaster.send_soon("feed_reload", {})

    @staticmethod
    def _unlink_all(paths: list[str]) -> None:
        for p in paths:
            try:
                Path(p).unlink(missing_ok=True)
            except OSError as e:
                log.warning("could not delete %s: %s", p, e)
