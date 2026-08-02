"""Configuration loading (TOML)."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

WHISPER_MODELS = ("tiny.en", "tiny", "base.en", "base", "small.en", "small",
                  "medium.en", "medium", "large-v3", "distil-large-v3")


@dataclass
class Config:
    # [node]
    node_number: str = ""
    callsign: str = ""
    # names for private/unregistered node numbers, e.g. your bridges
    node_aliases: dict = field(default_factory=dict)
    # optional override for the header's "Node <n>" subline — set it to name
    # several linked nodes, e.g. "Nodes 610750, 610751, & 610752". Blank falls
    # back to "Node <node_number>".
    node_label: str = ""

    # [usrp]
    usrp_bind: str = "0.0.0.0"
    usrp_port: int = 32001

    # [audio]
    data_dir: Path = Path("/var/lib/squelch")
    retention_days: int = 30          # 0 = keep forever
    min_free_gb: float = 2.0          # auto-purge oldest audio below this
    squelch_tail_ms: int = 400
    min_tx_ms: int = 300
    max_tx_secs: int = 300

    # [transcribe]
    transcribe_enabled: bool = True
    whisper_model: str = "base.en"
    whisper_device: str = "cpu"
    whisper_compute: str = "int8"
    language: str = "en"
    # don't transcribe (or voice-ID) transmissions that are just an MDC
    # data burst with no speech
    skip_mdc_only: bool = True
    # short natural-language context hint fed to whisper. Keep it a
    # SENTENCE, not a list — Whisper treats the prompt as prior text and
    # will happily continue a comma-separated list of callsigns as a
    # hallucination on marginal audio. Leave empty if unsure.
    initial_prompt: str = ""
    # append known speakers' callsigns to the prompt. OFF by default:
    # it primes whisper to spew callsign lists and creates a feedback
    # loop (bad transcript -> fake speaker -> longer list -> worse).
    auto_prompt_callsigns: bool = False

    # [speaker_id]
    speaker_id_enabled: bool = True
    # voice embedder: "titanet" (ONNX + GPU, best accuracy; needs the
    # exported titanet_large.onnx in data_dir/models — explicit opt-in,
    # switching engines requires re-enrolling voiceprints), "ecapa"
    # (speechbrain), "resemblyzer" (legacy fallback), or "auto"
    # (ecapa when installed, else resemblyzer; never titanet)
    embedder: str = "auto"
    # voice decision (top-K cosine + margin). Auto-assign a name only when
    # the best match's cosine clears assign_cos AND beats the runner-up by
    # assign_margin — the margin is what stops several people collapsing onto
    # one profile. Between suggest_cos and assign it shows a confirmable
    # "possible: X" chip; below, it stays Unknown. An Unknown beats a wrong
    # name. (Tune these to trade recall vs precision.)
    assign_cos: float = 0.84
    assign_margin: float = 0.05
    suggest_cos: float = 0.78
    # identity-resolution corroboration bands. ALL cosine knobs are
    # engine-scale-dependent: resemblyzer/ecapa same-speaker cosines run
    # ~0.78-0.92 (these defaults); TitaNet spreads far wider (same-speaker
    # median ~0.43 on narrowband FM) and needs much lower values — see
    # tools/migrate_titanet.py's calibration report.
    veto_cos: float = 0.55          # owner scoring below this vs voice -> veto the callsign
    cs_agree_cos: float = 0.70      # voice consistency floor to confirm a spoken callsign
    enroll_agree_cos: float = 0.78  # voice must reach this to enroll on a callsign
    learn_cos: float = 0.88         # self-learning bar for a voice-only assignment
    # only auto-create a new "Speaker N" for a voice that doesn't resemble
    # anyone known (best cosine below this). A near-miss above it is treated
    # as a probable thin-profile regular and left Unknown, not forked into a
    # new cluster. Engine-scale-dependent (TitaNet cross-speaker median ~0.20).
    autocluster_max_cos: float = 0.40
    # legacy raw-cosine thresholds, used only by the resemblyzer fallback;
    # band-limited radio audio (even wideband FM reaches the monitor as
    # 8 kHz PCM) compresses similarities upward, so these stay strict
    match_threshold: float = 0.86
    learn_threshold: float = 0.92
    autocluster: bool = True
    min_embed_ms: int = 1500
    # use callsigns heard in the transcript to identify/enroll speakers
    # (as tiered evidence — an explicit "this is X" can assign, subject to
    # a voice veto; anything weaker only feeds suggestions)
    use_callsigns: bool = True

    # [qso] — conversation session tracking (see qso.py). Attribution matches
    # the active QSO roster first and only falls back to the full speaker
    # database when the roster can't answer, which stops mid-QSO attribution
    # drifting to the wrong operators. All thresholds live here.
    qso_enabled: bool = True
    # a transmission after this much silence starts a new QSO
    qso_gap_secs: float = 120.0
    # a directed call ("W9XYZ this is W9ABC", "calling", "listening") after at
    # least this much idle also starts a new QSO
    qso_directed_idle_secs: float = 45.0
    # a sign-off ("73", "clear", "final") closes the current QSO
    qso_signoff_ends: bool = True
    # a roster member unheard for this long decays out and stops being matched
    qso_decay_mins: float = 15.0
    # turn-taking prior: the max cosine bonus given to the roster member who
    # has been silent longest (the likely next speaker); a weighting, not a rule
    qso_turn_prior_weight: float = 0.04
    # relaxed voice bar for a roster member (being in the conversation is
    # corroborating context), and the adjusted margin it must beat
    qso_roster_assign_cos: float = 0.80
    qso_roster_margin: float = 0.04

    # [voter]
    voter_sources: list = field(default_factory=list)
    voter_min_interval: float = 0.1
    # only poll each voter source while its node is actually linked on the
    # Pi (reported via /api/link by tools/source_monitor.py). Off = poll
    # 24/7 (original behavior). When on but the Pi reporter goes silent, it
    # fails open (keeps polling) so a dead reporter doesn't kill capture.
    voter_gate_on_connect: bool = False
    # idle timeout: also pause polling after this many minutes with no
    # received audio, resuming on the next transmission. Runtime-toggleable
    # from the admin UI (the DB setting overrides these defaults).
    voter_idle_timeout: bool = False
    voter_idle_minutes: float = 10.0
    # master kill switch: fully stop polling every voter source (runtime-
    # toggleable from the admin UI; DB setting overrides this default)
    voter_polling_disabled: bool = False
    # 32-hex-char AES-128 key for encrypted voter streams (empty =
    # plaintext stream)
    voter_key: str = ""
    # receiver site coordinates for the (fuzzy) geolocation map:
    # {"Crown_Point": [lat, lon], ...}. Empty = no map.
    voter_receivers: dict = field(default_factory=dict)

    # [callbook]
    # enrich heard callsigns with FCC data (name/QTH/class) via callook.info
    callbook_enabled: bool = True
    callbook_cache_days: int = 30     # re-check a callsign at most this often

    # [mdc]
    mdc_enabled: bool = True
    # optional shared secret required on POST /api/mdc from the node's
    # forwarder; empty = accept unauthenticated (LAN trust)
    mdc_forward_token: str = ""

    # [dtmf] — decode DTMF key presses (native chan_usrp frames when the
    # node sends them, Goertzel over the audio otherwise) onto each tx
    # and as live keypad events
    dtmf_enabled: bool = True

    # [captions] — provisional live captions streamed while a
    # transmission is still in progress (its own small whisper model;
    # the pipeline's full transcript replaces them on the card). Off by
    # default: only worth switching on where whisper runs fast (GPU).
    captions_enabled: bool = False
    captions_model: str = "small.en"
    # empty = inherit the [transcribe] device / compute_type
    captions_device: str = ""
    captions_compute: str = ""
    captions_interval: float = 0.7    # seconds between incremental decodes
    captions_window_secs: float = 30.0  # decode at most this much tail audio

    # [bandscope] — stream an FFT of the receive audio as a live waterfall.
    # Cheap (one 512-pt rfft ~30x/sec while keyed); the browser renders it.
    bandscope_enabled: bool = False

    # [timemachine] — the DVR: scrub the day's overs on one clock in the browser.
    timemachine_enabled: bool = False

    # [export] — the Time Machine's server-side render: stitch a time range of
    # overs into one MP4 (waveform + burned captions), queued behind whisper.
    export_enabled: bool = False
    export_max_span_secs: float = 1800.0    # cap one render at 30 min of timeline
    export_video_height: int = 480

    # [sayagain] — cross-source callsign resolution (fuse Whisper + voiceprint +
    # self-ID + QRZ to correct busted calls) plus tap-to-loop replay on the card
    sayagain_enabled: bool = False

    # [web]
    web_bind: str = "0.0.0.0"
    web_port: int = 8080
    admin_password: str = ""
    # fallback only — once set from the admin settings panel, the DB
    # value wins
    site_name: str = "Squelch"
    footer_text: str = ""

    @property
    def audio_dir(self) -> Path:
        return self.data_dir / "audio"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "squelch.db"


def load_config(path: str | Path) -> Config:
    with open(path, "rb") as f:
        raw = tomllib.load(f)

    cfg = Config()
    node = raw.get("node", {})
    cfg.node_number = str(node.get("number", cfg.node_number))
    cfg.callsign = node.get("callsign", cfg.callsign)
    cfg.node_aliases = {str(k): str(v)
                        for k, v in node.get("aliases", {}).items()}
    cfg.node_label = str(node.get("label", cfg.node_label))

    usrp = raw.get("usrp", {})
    cfg.usrp_bind = usrp.get("bind", cfg.usrp_bind)
    cfg.usrp_port = int(usrp.get("port", cfg.usrp_port))

    audio = raw.get("audio", {})
    cfg.data_dir = Path(audio.get("data_dir", cfg.data_dir))
    cfg.retention_days = int(audio.get("retention_days", cfg.retention_days))
    cfg.min_free_gb = float(audio.get("min_free_gb", cfg.min_free_gb))
    cfg.squelch_tail_ms = int(audio.get("squelch_tail_ms", cfg.squelch_tail_ms))
    cfg.min_tx_ms = int(audio.get("min_tx_ms", cfg.min_tx_ms))
    cfg.max_tx_secs = int(audio.get("max_tx_secs", cfg.max_tx_secs))

    tr = raw.get("transcribe", {})
    cfg.transcribe_enabled = bool(tr.get("enabled", cfg.transcribe_enabled))
    cfg.whisper_model = tr.get("model", cfg.whisper_model)
    cfg.whisper_device = tr.get("device", cfg.whisper_device)
    cfg.whisper_compute = tr.get("compute_type", cfg.whisper_compute)
    cfg.language = tr.get("language", cfg.language)
    cfg.skip_mdc_only = bool(tr.get("skip_mdc_only", cfg.skip_mdc_only))
    cfg.initial_prompt = tr.get("initial_prompt", cfg.initial_prompt)
    cfg.auto_prompt_callsigns = bool(
        tr.get("auto_prompt_callsigns", cfg.auto_prompt_callsigns))

    sp = raw.get("speaker_id", {})
    cfg.speaker_id_enabled = bool(sp.get("enabled", cfg.speaker_id_enabled))
    cfg.embedder = str(sp.get("embedder", cfg.embedder))
    cfg.assign_cos = float(sp.get("assign_cos", cfg.assign_cos))
    cfg.assign_margin = float(sp.get("assign_margin", cfg.assign_margin))
    cfg.suggest_cos = float(sp.get("suggest_cos", cfg.suggest_cos))
    cfg.veto_cos = float(sp.get("veto_cos", cfg.veto_cos))
    cfg.cs_agree_cos = float(sp.get("cs_agree_cos", cfg.cs_agree_cos))
    cfg.enroll_agree_cos = float(
        sp.get("enroll_agree_cos", cfg.enroll_agree_cos))
    cfg.learn_cos = float(sp.get("learn_cos", cfg.learn_cos))
    cfg.autocluster_max_cos = float(
        sp.get("autocluster_max_cos", cfg.autocluster_max_cos))
    cfg.match_threshold = float(sp.get("match_threshold", cfg.match_threshold))
    cfg.learn_threshold = float(sp.get("learn_threshold", cfg.learn_threshold))
    cfg.autocluster = bool(sp.get("autocluster", cfg.autocluster))
    cfg.min_embed_ms = int(sp.get("min_embed_ms", cfg.min_embed_ms))
    cfg.use_callsigns = bool(sp.get("use_callsigns", cfg.use_callsigns))

    qso = raw.get("qso", {})
    cfg.qso_enabled = bool(qso.get("enabled", cfg.qso_enabled))
    cfg.qso_gap_secs = float(qso.get("gap_secs", cfg.qso_gap_secs))
    cfg.qso_directed_idle_secs = float(
        qso.get("directed_idle_secs", cfg.qso_directed_idle_secs))
    cfg.qso_signoff_ends = bool(qso.get("signoff_ends", cfg.qso_signoff_ends))
    cfg.qso_decay_mins = float(qso.get("decay_mins", cfg.qso_decay_mins))
    cfg.qso_turn_prior_weight = float(
        qso.get("turn_prior_weight", cfg.qso_turn_prior_weight))
    cfg.qso_roster_assign_cos = float(
        qso.get("roster_assign_cos", cfg.qso_roster_assign_cos))
    cfg.qso_roster_margin = float(
        qso.get("roster_margin", cfg.qso_roster_margin))

    voter = raw.get("voter", {})
    cfg.voter_sources = [str(u) for u in voter.get("sources", [])]
    cfg.voter_min_interval = float(voter.get("min_interval",
                                             cfg.voter_min_interval))
    cfg.voter_gate_on_connect = bool(
        voter.get("gate_on_connect", cfg.voter_gate_on_connect))
    cfg.voter_idle_timeout = bool(
        voter.get("idle_timeout", cfg.voter_idle_timeout))
    cfg.voter_idle_minutes = float(
        voter.get("idle_minutes", cfg.voter_idle_minutes))
    cfg.voter_polling_disabled = bool(
        voter.get("disabled", cfg.voter_polling_disabled))
    cfg.voter_key = str(voter.get("key", cfg.voter_key))
    cfg.voter_receivers = {
        str(k): [float(v[0]), float(v[1])]
        for k, v in voter.get("receivers", {}).items()
        if isinstance(v, (list, tuple)) and len(v) == 2}

    cb = raw.get("callbook", {})
    cfg.callbook_enabled = bool(cb.get("enabled", cfg.callbook_enabled))
    cfg.callbook_cache_days = int(cb.get("cache_days", cfg.callbook_cache_days))

    mdc = raw.get("mdc", {})
    cfg.mdc_enabled = bool(mdc.get("enabled", cfg.mdc_enabled))
    cfg.mdc_forward_token = mdc.get("forward_token", cfg.mdc_forward_token)

    dt = raw.get("dtmf", {})
    cfg.dtmf_enabled = bool(dt.get("enabled", cfg.dtmf_enabled))

    cap = raw.get("captions", {})
    cfg.captions_enabled = bool(cap.get("enabled", cfg.captions_enabled))
    cfg.captions_model = str(cap.get("model", cfg.captions_model))
    cfg.captions_device = str(cap.get("device", cfg.captions_device))
    cfg.captions_compute = str(cap.get("compute_type", cfg.captions_compute))
    cfg.captions_interval = float(cap.get("interval", cfg.captions_interval))
    cfg.captions_window_secs = float(
        cap.get("window_secs", cfg.captions_window_secs))

    bs = raw.get("bandscope", {})
    cfg.bandscope_enabled = bool(bs.get("enabled", cfg.bandscope_enabled))

    tm = raw.get("timemachine", {})
    cfg.timemachine_enabled = bool(tm.get("enabled", cfg.timemachine_enabled))

    ex = raw.get("export", {})
    cfg.export_enabled = bool(ex.get("enabled", cfg.export_enabled))
    cfg.export_max_span_secs = float(
        ex.get("max_span_secs", cfg.export_max_span_secs))
    cfg.export_video_height = int(
        ex.get("video_height", cfg.export_video_height))

    sa = raw.get("sayagain", {})
    cfg.sayagain_enabled = bool(sa.get("enabled", cfg.sayagain_enabled))

    web = raw.get("web", {})
    cfg.web_bind = web.get("bind", cfg.web_bind)
    cfg.web_port = int(web.get("port", cfg.web_port))
    cfg.admin_password = web.get("admin_password", cfg.admin_password)
    cfg.site_name = web.get("site_name", cfg.site_name)
    cfg.footer_text = web.get("footer_text", cfg.footer_text)

    return cfg
