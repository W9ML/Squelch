"""FastAPI application: REST API, WebSocket feed, static UI."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
import re
import shutil
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from fastapi import (FastAPI, HTTPException, Request, UploadFile, WebSocket,
                     WebSocketDisconnect)
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                                Response)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import (__version__, elevenlabs_stt, geo, qrz, tabexport, usrp,
               watchdog, webpush)
from .auth import SESSION_COOKIE, SESSION_TTL, AuthManager
from .callsigns import CALL_RE
from .config import Config, WHISPER_MODELS
from .db import Database
from .bandscope import LiveBandscope
from .dtmf import LiveDTMF
from .events import AudioTee, Broadcaster, LiveAudioBroadcaster
from .export import Exporter
from .livecaption import LiveCaptioner
from .nodedb import NodeDB
from .pipeline import Pipeline

log = logging.getLogger(__name__)

# the built Next.js frontend (`next build` static export). Override with
# SQUELCH_WEB_DIR.
WEB_OUT_DIR = Path(os.environ.get("SQUELCH_WEB_DIR")
                   or Path(__file__).parent.parent / "web" / "out")

# "night" is intentionally NOT listed — its CSS is kept (see globals.css) but
# it's retired from the picker. Paper is first so it's the fallback default.
THEMES = ("paper", "crt", "amber", "cyan", "borland", "c64", "day")

# how long to keep closed rows in the connection log
_CONNLOG_RETENTION_DAYS = 30

LOGO_TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/svg+xml": ".svg",
    "image/webp": ".webp",
}
LOGO_MAX_BYTES = 512 * 1024

# per-user profile pictures (the account tile). Raster only — no SVG, so no
# script-in-image concern for a per-account upload.
AVATAR_TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
}
AVATAR_MAX_BYTES = 1024 * 1024

# Content-Security-Policy for the app UI. script-src is strict 'self' only
# (plus the Next export's inline-bootstrap hashes, added at startup), so an
# injected inline <script> or onerror handler can't execute — defense in
# depth behind output escaping. The remaining external origins are the
# font / map-tile / photo CDNs the UI loads. This is NOT applied to
# /api/logo, which keeps its own stricter 'sandbox' policy.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com; "
    "img-src 'self' data: blob: "
    "https://*.basemaps.cartocdn.com "
    # QRZ profile photos (hosted on qrz.com CDNs and their S3 bucket)
    "https://*.qrz.com https://s3.amazonaws.com; "
    "connect-src 'self'; "
    # the PWA service worker and its web-app manifest (both same-origin)
    "worker-src 'self'; manifest-src 'self'; "
    "object-src 'none'; base-uri 'self'; form-action 'self'; "
    "frame-ancestors 'none'"
)

# an inline <script> block (no src=) and its exact byte content
_INLINE_SCRIPT_RE = re.compile(
    rb"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>",
    re.DOTALL | re.IGNORECASE)


# at-a-glance voter status page (rendered when /api/link is opened in a
# browser). CSS is static so it needs no CSP exceptions; a meta refresh keeps
# it live without inline script.
_VOTER_PAGE_CSS = (
    "body{margin:0;background:#0c1016;color:#d7dee6;"
    "font-family:ui-monospace,'Cascadia Mono',Consolas,monospace;"
    "display:flex;min-height:100vh;align-items:center;justify-content:center;}"
    ".card{background:#141a22;border:1px solid #232b36;border-radius:14px;"
    "padding:26px 30px;min-width:300px;box-shadow:0 18px 50px rgba(0,0,0,.5);}"
    "h1{font-size:.72rem;letter-spacing:.16em;text-transform:uppercase;"
    "color:#5f6b7a;margin:0 0 16px;}"
    ".badge{display:block;text-align:center;font-size:1.5rem;font-weight:700;"
    "padding:14px;border-radius:10px;}"
    "table{width:100%;border-collapse:collapse;margin-top:16px;font-size:.9rem;}"
    "td{padding:6px 0;border-top:1px solid #1a212b;}"
    "td:last-child{text-align:right;color:#9aa6b4;}"
    ".dot{display:inline-block;width:9px;height:9px;border-radius:50%;"
    "margin-right:8px;vertical-align:middle;}"
    ".meta{margin-top:16px;font-size:.78rem;color:#9aa6b4;line-height:1.8;}"
    ".meta b{color:#d7dee6;font-weight:600;}"
    ".home{display:inline-block;color:#63b3ed;text-decoration:none;"
    "font-size:.8rem;margin-bottom:14px;}"
    ".home:hover{text-decoration:underline;}"
    ".controls{margin-top:16px;border-top:1px solid #1a212b;padding-top:12px;}"
    ".controls label{display:flex;align-items:center;gap:9px;font-size:.86rem;"
    "padding:5px 0;cursor:pointer;}"
    ".controls label.off{opacity:.45;}"
    ".controls input{width:16px;height:16px;accent-color:#6fce8f;cursor:pointer;}"
    ".controls input:disabled{cursor:default;}"
    ".foot{margin-top:16px;font-size:.68rem;color:#5f6b7a;text-align:center;}"
)
# summary -> (label, color). Uses only the U+25CF dot (in every font); color
# carries the state, so no exotic glyphs that render as tofu.
_VOTER_BADGE = {
    "polling": ("● POLLING LIVE", "#6fce8f"),
    "paused": ("● PAUSED", "#f0b429"),
    "partial": ("● PARTIAL", "#f0b429"),
    "disabled": ("● DISABLED", "#e0685a"),
    "none": ("NO VOTER SOURCES", "#8a95a3"),
}


def _render_voter_page(snap: dict) -> str:
    import html as _html

    def esc(x):
        return _html.escape(str(x))

    label, color = _VOTER_BADGE.get(snap["summary"], _VOTER_BADGE["paused"])
    reason_txt = {"polling": "polling (live)", "idle": "paused — channel idle",
                  "disabled": "disabled"}
    rows = ""
    for s in snap["sources"]:
        r = s["reason"]
        txt = (f"paused — node {esc(s['node'])} not linked"
               if r == "unlinked" else reason_txt.get(r, r))
        dot = "#6fce8f" if s["polling"] else (
            "#e0685a" if r == "disabled" else "#f0b429")
        rows += (f'<tr><td><span class="dot" style="background:{dot}"></span>'
                 f'node {esc(s["node"] or "?")}</td><td>{esc(txt)}</td></tr>')
    idle = (f'on · {esc(snap["idle_minutes"])} min'
            if snap["idle_timeout"] else "off")
    idle_for = snap.get("idle_for_secs")
    idle_for_txt = (f"{idle_for:.0f}s since last audio"
                    if idle_for is not None else "no audio yet")
    linked = ", ".join(esc(n) for n in snap["linked_nodes"]) or "none"
    rep = snap.get("link_reported_at")
    rep_txt = f"{time.time() - rep:.0f}s ago" if rep else "never"
    now = datetime.now().strftime("%H:%M:%S")
    # the same two toggles as Settings; the idle box greys out while polling
    # is fully disabled (the disable switch overrides it)
    vd = "checked" if snap["disabled"] else ""
    vi = "checked" if snap["idle_timeout"] else ""
    vi_dis = "disabled" if snap["disabled"] else ""
    idle_off_cls = " off" if snap["disabled"] else ""
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        "<title>Voter polling — Squelch</title>"
        "<meta http-equiv=\"refresh\" content=\"5\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<style>{_VOTER_PAGE_CSS}</style></head><body><div class=\"card\">"
        '<a class="home" href="/">&larr; Back to Squelch</a>'
        "<h1>Squelch · Voter polling</h1>"
        f'<div class="badge" style="color:{color};background:'
        f'color-mix(in srgb,{color} 12%,#141a22);border:1px solid {color}">'
        f"{label}</div>"
        f"<table>{rows}</table>"
        '<div class="meta">'
        f"<div>Idle timeout: <b>{idle}</b> · <b>{idle_for_txt}</b></div>"
        f"<div>Linked nodes: <b>{linked}</b> · Pi report <b>{rep_txt}</b></div>"
        "</div>"
        '<div class="controls">'
        f'<label><input type="checkbox" id="vd" {vd}> '
        "Disable voter polling entirely</label>"
        f'<label class="ctl-idle{idle_off_cls}">'
        f'<input type="checkbox" id="vi" {vi} {vi_dis}> '
        "Pause polling when idle</label>"
        "</div>"
        f'<div class="foot">auto-refreshes every 5s · as of {now}</div>'
        '</div><script src="/api/link.js"></script></body></html>')


# shown (in place of the raw 403 JSON) when a browser hits the voter status
# page without an admin session. Just a looping GIF, centered.
_DENIED_GIF = "https://media.giphy.com/media/3ohzdQ1IynzclJldUQ/giphy.gif"
# scoped policy for this one page: only the giphy image + inline CSS, nothing
# else. Set on the response so the app's global CSP is left untouched.
_DENIED_CSP = ("default-src 'none'; img-src https://*.giphy.com; "
               "style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'")


def _render_denied_page() -> str:
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        "<title>Admins only — Squelch</title>"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<style>html,body{height:100%;margin:0;}"
        "body{background:#0c1016;display:flex;flex-direction:column;"
        "align-items:center;justify-content:center;gap:18px;"
        "font-family:ui-monospace,'Cascadia Mono',Consolas,monospace;"
        "color:#5f6b7a;}"
        "img{max-width:min(92vw,520px);max-height:70vh;border-radius:12px;}"
        ".cap{font-size:.85rem;letter-spacing:.08em;}"
        "a{color:#63b3ed;text-decoration:none;font-size:.8rem;}"
        "a:hover{text-decoration:underline;}</style></head><body>"
        f'<img src="{_DENIED_GIF}" alt="admins only">'
        '<div class="cap">admin login required</div>'
        '<a href="/">&larr; back to Squelch</a>'
        "</body></html>")


# served from a same-origin file so the strict CSP (script-src 'self') allows
# it without an inline-script hash. Wires the two checkboxes to the same
# settings endpoints the admin UI uses, then reloads to reflect the new state.
_VOTER_PAGE_JS = (
    "(function(){"
    "function post(u,b){return fetch(u,{method:'POST',"
    "headers:{'Content-Type':'application/json'},credentials:'same-origin',"
    "body:JSON.stringify(b)}).then(function(){location.reload();});}"
    "function wire(id,fn){var e=document.getElementById(id);"
    "if(e)e.addEventListener('change',function(){fn(e.checked);});}"
    "wire('vd',function(on){post('/api/settings/voter_disabled',{disabled:on});});"
    "wire('vi',function(on){post('/api/settings/voter_idle',{enabled:on});});"
    "})();"
)


def _peer_ip(headers, client) -> str | None:
    """Real client IP behind a local reverse proxy / tunnel: nginx and
    cloudflared both set X-Forwarded-For (nginx also X-Real-IP), so prefer
    those over the immediate peer (which is just the proxy on localhost)."""
    xff = headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip() or None
    xri = headers.get("x-real-ip")
    if xri:
        return xri.strip() or None
    return client.host if client else None


def _csp_with_next_hashes() -> str:
    """The CSP, extended with sha256 hashes for the Next.js export's inline
    bootstrap scripts. A static export can't run without them, and hashing
    keeps script-src free of 'unsafe-inline' — only those exact scripts may
    run inline, so injected content (transcripts come off the air) still
    can't execute. Recomputed at startup, so a redeployed build just works."""
    hashes: set[str] = set()
    for page in WEB_OUT_DIR.glob("*.html"):
        try:
            data = page.read_bytes()
        except OSError:
            continue
        for m in _INLINE_SCRIPT_RE.finditer(data):
            body = m.group(1)
            if body.strip():
                digest = base64.b64encode(hashlib.sha256(body).digest()).decode()
                hashes.add(f"'sha256-{digest}'")
    if not hashes:
        return CONTENT_SECURITY_POLICY
    return CONTENT_SECURITY_POLICY.replace(
        "script-src 'self'",
        "script-src 'self' " + " ".join(sorted(hashes)), 1)


class LoginBody(BaseModel):
    username: str = "admin"
    password: str


class SetupBody(BaseModel):
    username: str
    password: str
    callsign: str = ""
    node: str = ""
    footer: str = ""


class LabelBody(BaseModel):
    label: str


class ModelBody(BaseModel):
    model: str


class TextBody(BaseModel):
    text: str


class BrandBody(BaseModel):
    callsign: str = ""
    node: str = ""


class QrzBody(BaseModel):
    username: str = ""
    password: str = ""


class ThemeBody(BaseModel):
    theme: str


class AssignBody(BaseModel):
    speaker_id: int | None = None   # existing speaker...
    label: str | None = None        # ...or find/create by label


class MDCBody(BaseModel):
    unit: str                       # unit ID as app_rpt logged it
    type: str = "I"                 # I/E/S/C
    node: str | None = None
    ts: float | None = None         # optional; squelch uses arrival time


class SourceBody(BaseModel):
    source: str                     # "local" or "node"
    node: str | None = None         # node number when source == "node"
    hub: str | None = None          # hub that reported it (e.g. 610750/751/752)
    ts: float | None = None


class LinkBody(BaseModel):
    # the AllStar nodes the monitored node is currently linked to; sent
    # periodically by the Pi's source monitor to gate voter polling
    connected: list[str] = []
    hub: str | None = None          # reporting hub (610750/751/752), for per-hub status


class VoterIdleBody(BaseModel):
    enabled: bool
    minutes: float | None = None    # optional; keeps the current value if None


class VoterDisableBody(BaseModel):
    disabled: bool


class UserBody(BaseModel):
    username: str
    password: str
    role: str = "admin"


class RoleBody(BaseModel):
    role: str


class PasswordBody(BaseModel):
    current: str
    new: str


class PurgeBody(BaseModel):
    start: float                    # epoch seconds, inclusive
    end: float                     # epoch seconds, exclusive


class TMExportBody(BaseModel):
    start: float                    # epoch seconds, inclusive
    end: float                     # epoch seconds, exclusive
    captions: bool = True


class WatchBody(BaseModel):
    kind: str                       # callsign | mdc_unit | emergency | speaker
    value: str = ""
    label: str = ""
    webhook: str = ""


class WatchEnableBody(BaseModel):
    enabled: bool


class PushKeys(BaseModel):
    p256dh: str
    auth: str


class PushSubBody(BaseModel):
    endpoint: str
    keys: PushKeys


class PushUnsubBody(BaseModel):
    endpoint: str


class MDCUnitBody(BaseModel):
    unit: str                       # MDC unit ID as decoded (unit_raw)
    speaker_id: int


# ---- cases (investigative case management) ----
CASE_STATUSES = ("open", "active", "suspended", "closed", "referred")


class CaseBody(BaseModel):
    title: str
    subject: str = ""
    summary: str = ""


class CaseUpdateBody(BaseModel):
    title: str | None = None
    status: str | None = None
    subject: str | None = None
    summary: str | None = None


class CaseItemBody(BaseModel):
    tx_id: int
    label: str = ""
    note: str = ""


class CaseNoteBody(BaseModel):
    text: str


def _render_case_report(case: dict, site_name: str) -> str:
    """Self-contained printable HTML documentation packet for one case."""
    import html as _html

    def esc(x) -> str:
        return _html.escape(str(x if x is not None else ""))

    def ts(t) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) if t else "—"

    css = (
        "body{font:14px/1.5 system-ui,'Segoe UI',Arial,sans-serif;color:#111;"
        "max-width:900px;margin:2rem auto;padding:0 1rem}"
        "h1{margin:0 0 .2rem}h3{margin:1.4rem 0 .4rem}"
        ".sub{color:#555;margin-bottom:1.4rem}"
        "table{border-collapse:collapse;width:100%;margin:.4rem 0 1rem;font-size:13px}"
        "th,td{border:1px solid #ccc;padding:.35rem .5rem;text-align:left;vertical-align:top}"
        "th{background:#f2f2f2}"
        "dl{display:grid;grid-template-columns:140px 1fr;gap:.2rem .75rem}"
        "dt{color:#555;font-weight:600}dd{margin:0}"
        ".log{border-left:3px solid #ccc;padding:.2rem .75rem;margin:.35rem 0}"
        ".lt{color:#777;font-variant-numeric:tabular-nums}.la{font-weight:600}"
        "@media print{body{margin:0}}"
    )
    rows = ""
    for i, it in enumerate(case.get("items", []), 1):
        secs = round((it.get("duration_ms") or 0) / 1000.0, 1)
        purged = "" if it.get("has_audio") else " (audio purged)"
        rows += (f"<tr><td>{i}</td><td>{ts(it['started_at'])}</td><td>{secs}s</td>"
                 f"<td>{esc(it.get('origin'))}</td><td>{esc(it.get('origin_hub'))}</td>"
                 f"<td>tx {it['tx_id']}{purged}</td><td>{esc(it.get('label'))}</td>"
                 f"<td>{esc(it.get('note'))}</td></tr>")
    if not rows:
        rows = "<tr><td colspan='8'>No recordings attached.</td></tr>"
    log = ""
    for n in case.get("notes", []):
        sysflag = " · system" if n.get("kind") == "system" else ""
        log += (f"<div class='log'><span class='lt'>{ts(n['ts'])}</span> "
                f"<span class='la'>{esc(n.get('author') or 'system')}</span>{sysflag}"
                f"<br>{esc(n['text'])}</div>")
    if not log:
        log = "<p>—</p>"
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>Case {esc(case['number'])} — {esc(case['title'])}</title>"
        f"<style>{css}</style></head><body>"
        f"<h1>Case {esc(case['number'])} — {esc(case['title'])}</h1>"
        f"<div class='sub'>{esc(site_name)} · interference documentation · "
        f"generated {ts(time.time())}</div>"
        "<dl>"
        f"<dt>Status</dt><dd>{esc(case['status'])}</dd>"
        f"<dt>Subject</dt><dd>{esc(case.get('subject')) or '—'}</dd>"
        f"<dt>Opened</dt><dd>{ts(case['opened_at'])}</dd>"
        f"<dt>Evidence</dt><dd>{len(case.get('items', []))} recording(s)</dd>"
        "</dl>"
        f"<h3>Summary</h3><p>{esc(case.get('summary')) or '—'}</p>"
        "<h3>Evidence — recordings</h3><table><thead><tr>"
        "<th>#</th><th>Timestamp (local)</th><th>Length</th><th>Origin</th>"
        "<th>Hub</th><th>Recording</th><th>Label</th><th>Note</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
        f"<h3>Activity log</h3>{log}"
        "</body></html>"
    )


class _LoginThrottle:
    """Per-client-IP login rate limiting (in-memory, single-process).

    After `max_fails` failures from an IP the next attempts are locked out
    with exponential backoff (capped at `max_lock`). A success, or a quiet
    `window`, clears the counter. Defangs online password guessing; it is
    not a substitute for a strong password. Note: behind a reverse proxy
    every request carries the proxy's IP, so tune/observe accordingly.
    """

    def __init__(self, max_fails: int = 5, window: float = 900.0,
                 base_lock: float = 5.0, max_lock: float = 900.0):
        self.max_fails = max_fails
        self.window = window
        self.base_lock = base_lock
        self.max_lock = max_lock
        self._data: dict[str, dict] = {}

    def retry_after(self, ip: str, now: float) -> float:
        e = self._data.get(ip)
        return e["until"] - now if e and e["until"] > now else 0.0

    def record_failure(self, ip: str, now: float) -> None:
        e = self._data.get(ip)
        if e is None or now - e["ts"] > self.window:
            e = {"count": 0, "until": 0.0, "ts": now}
            self._data[ip] = e
        e["count"] += 1
        e["ts"] = now
        if e["count"] >= self.max_fails:
            over = e["count"] - self.max_fails
            e["until"] = now + min(self.base_lock * (2 ** over), self.max_lock)
        if len(self._data) > 4096:            # bound memory under attack
            self._data = {k: v for k, v in self._data.items()
                          if v["until"] > now or now - v["ts"] < self.window}

    def record_success(self, ip: str) -> None:
        self._data.pop(ip, None)


def create_app(cfg: Config) -> FastAPI:
    db = Database(cfg.db_path)
    # Say Again: attach cross-source callsign resolution to fetched overs
    db.resolve_calls = cfg.sayagain_enabled
    broadcaster = Broadcaster()
    pipeline = Pipeline(cfg, db, broadcaster)
    exporter = Exporter(cfg, db, pipeline)
    auth = AuthManager(db, cfg.data_dir, cfg.admin_password)
    login_throttle = _LoginThrottle()
    nodedb = NodeDB(cfg.data_dir, cfg.node_aliases)
    branding_dir = cfg.data_dir / "branding"
    avatar_dir = cfg.data_dir / "avatars"
    live_audio = LiveAudioBroadcaster()
    # real-time frame taps beside the browser stream: live DTMF keypad
    # events and (when enabled) streaming captions
    live_sinks: list = [live_audio]
    if cfg.dtmf_enabled:
        live_sinks.append(LiveDTMF(broadcaster))
    captioner: LiveCaptioner | None = None
    if cfg.captions_enabled and cfg.transcribe_enabled:
        captioner = LiveCaptioner(cfg, broadcaster)
        live_sinks.append(captioner)
    if cfg.bandscope_enabled:
        live_sinks.append(LiveBandscope(broadcaster))
    segmenter = usrp.Segmenter(
        on_start=pipeline.on_rx_start,
        on_complete=pipeline.on_transmission,
        on_discard=pipeline.on_rx_discard,
        squelch_tail_ms=cfg.squelch_tail_ms,
        min_tx_ms=cfg.min_tx_ms,
        max_tx_secs=cfg.max_tx_secs,
        live_audio=AudioTee(*live_sinks))
    # seed the last-audio clock from history so a restart on a box that HAS
    # received audio reads as "last audio Nh ago", not a misleading "no audio
    # yet" (which should only mean the node has never sent anything)
    _seed_ts = db.last_audio_ts()
    if _seed_ts:
        segmenter.last_frame_wall = _seed_ts
    pipeline.set_rx_probe(lambda: segmenter.active or pipeline.rx_active)
    # voter idle-timeout: feed the collector the channel-activity clock (last
    # USRP frame) and apply the runtime setting (DB overrides the toml default)
    pipeline.voter.set_activity_probe(lambda: segmenter.last_frame_wall)
    _idle_saved = db.get_setting("voter_idle_timeout")
    _idle_on = (_idle_saved == "on") if _idle_saved is not None \
        else cfg.voter_idle_timeout
    _idle_mins = db.get_setting("voter_idle_minutes")
    pipeline.voter.set_idle_timeout(
        _idle_on, float(_idle_mins) if _idle_mins else cfg.voter_idle_minutes)
    _disabled_saved = db.get_setting("voter_polling_disabled")
    pipeline.voter.set_disabled(
        (_disabled_saved == "on") if _disabled_saved is not None
        else cfg.voter_polling_disabled)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # any connection row still marked open belongs to a dead process
        # (this one just started) — close them, then trim old history
        db.close_open_connections(time.time())
        db.prune_connections(_CONNLOG_RETENTION_DAYS)
        transport = await usrp.start_listener(
            cfg.usrp_bind, cfg.usrp_port, segmenter)
        pipeline.voter.start()
        # warm the whisper model now (background) so the first
        # transmission after a restart doesn't hit a cold model
        if cfg.transcribe_enabled and pipeline.transcriber.available:
            pipeline.transcriber.ensure_model(pipeline.whisper_model)
        if captioner is not None and captioner.transcriber.available:
            captioner.transcriber.ensure_model(captioner.model)
        tasks = [
            asyncio.create_task(segmenter.run_watchdog()),
            asyncio.create_task(pipeline.run_worker()),
            asyncio.create_task(pipeline.run_transcribe_retry()),
            asyncio.create_task(pipeline.run_callbook_worker()),
            asyncio.create_task(pipeline.run_retention()),
            asyncio.create_task(nodedb.run_refresher()),
            # systemd watchdog: pings while the pipeline is healthy; a wedged
            # loop or a sustained GPU fault stops the pings -> systemd restarts
            asyncio.create_task(watchdog.run_pinger(pipeline.healthy)),
        ]
        if cfg.export_enabled:
            # the Time Machine render queue — yields to whisper, nices ffmpeg
            tasks.append(asyncio.create_task(exporter.run_worker()))
        if captioner is not None:
            tasks.append(asyncio.create_task(captioner.run()))
            if captioner.transcriber.available:
                # absorb the first-decode init cost off the critical path so
                # the first over after a restart isn't slow
                tasks.append(asyncio.create_task(captioner.warmup()))
        log.info("squelch %s ready — web on %s:%d, USRP on %s:%d",
                 __version__, cfg.web_bind, cfg.web_port,
                 cfg.usrp_bind, cfg.usrp_port)
        yield
        pipeline.voter.stop()
        transport.close()
        for t in tasks:
            t.cancel()
        db.close()

    app = FastAPI(title="squelch", version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None)
    app.state.db = db          # handy for tests
    app.state.auth = auth
    app.state.pipeline = pipeline

    # inline-script hashes for the Next.js frontend, computed once at startup
    csp = _csp_with_next_hashes()

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        path = request.url.path
        if path == "/":
            # cheap revalidation; avoids serving a stale index after upgrades
            response.headers["Cache-Control"] = "no-cache"
        # setdefault so endpoints with their own policy (e.g. /api/logo's
        # stricter 'sandbox' CSP) are not clobbered
        response.headers.setdefault("Content-Security-Policy", csp)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Permissions-Policy", "geolocation=(), microphone=(), camera=()")
        return response

    def current_user(request: Request) -> str | None:
        return auth.verify_token(request.cookies.get(SESSION_COOKIE))

    def require_admin(request: Request) -> str:
        user = current_user(request)
        if user is None:
            raise HTTPException(status_code=403, detail="admin login required")
        return user

    def require_super(request: Request) -> str:
        user = require_admin(request)
        if not auth.is_super(user):
            raise HTTPException(status_code=403,
                                detail="super admin required")
        return user

    def require_settings(request: Request) -> str:
        # settings are for super/admin; plain 'user' accounts are blocked
        user = require_admin(request)
        if not auth.can_settings(user):
            raise HTTPException(status_code=403,
                                detail="settings access required")
        return user

    def delete_audio_files(paths: list[str]) -> None:
        for p in paths:
            try:
                Path(p).unlink(missing_ok=True)
            except OSError as e:
                log.warning("could not delete %s: %s", p, e)

    # ---- pages ----

    @app.get("/")
    async def index():
        idx = WEB_OUT_DIR / "index.html"
        if idx.exists():
            # never let a browser pin the HTML: it names the content-hashed JS
            # chunks, so a cached index keeps loading stale app code after a
            # deploy (the _next/* chunks themselves are immutable + cacheable)
            return FileResponse(idx, headers={"Cache-Control": "no-cache"})
        raise HTTPException(status_code=404, detail="frontend not built")

    # ---- PWA assets (served from the export root with the right MIME/scope) ----

    @app.get("/sw.js")
    async def service_worker():
        p = WEB_OUT_DIR / "sw.js"
        if not p.exists():
            raise HTTPException(status_code=404, detail="not built")
        # a SW at /sw.js already controls scope '/', but set the header
        # explicitly and disable caching so updates roll out on reload
        return FileResponse(p, media_type="text/javascript", headers={
            "Service-Worker-Allowed": "/", "Cache-Control": "no-cache"})

    @app.get("/manifest.webmanifest")
    async def web_manifest():
        p = WEB_OUT_DIR / "manifest.webmanifest"
        if not p.exists():
            raise HTTPException(status_code=404, detail="not built")
        return FileResponse(p, media_type="application/manifest+json",
                            headers={"Cache-Control": "no-cache"})

    # ---- auth ----

    @app.post("/api/login")
    async def login(body: LoginBody, request: Request):
        # throttle on the real client IP: behind a local tunnel/proxy
        # (cloudflared, nginx) the socket peer is 127.0.0.1 for everyone,
        # which would merge all visitors into ONE throttle bucket — an
        # attacker's failures would lock legitimate users out
        ip = _peer_ip(request.headers, request.client) or "?"
        now = time.time()
        wait = login_throttle.retry_after(ip, now)
        if wait > 0:
            raise HTTPException(
                status_code=429,
                detail=f"too many attempts — wait {int(wait) + 1}s",
                headers={"Retry-After": str(int(wait) + 1)})
        username = body.username.strip() or "admin"
        ok = await asyncio.to_thread(auth.login, username, body.password)
        if not ok:
            login_throttle.record_failure(ip, now)
            raise HTTPException(status_code=403,
                                detail="wrong username or password")
        login_throttle.record_success(ip)
        resp = JSONResponse({"ok": True, "username": username})
        resp.set_cookie(SESSION_COOKIE, auth.issue_token(username),
                        max_age=SESSION_TTL, httponly=True, samesite="lax",
                        secure=request.url.scheme == "https")
        return resp

    @app.post("/api/logout")
    async def logout():
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(SESSION_COOKIE)
        return resp

    @app.post("/api/password")
    async def change_password(body: PasswordBody, request: Request):
        user = require_admin(request)
        try:
            await asyncio.to_thread(
                auth.change_password, user, body.current, body.new)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"ok": True}

    @app.post("/api/setup")
    async def first_time_setup(body: SetupBody, request: Request):
        # first-run only: create the super-admin + station identity. Self-closes
        # the moment any user exists, so it can't be used to add accounts later.
        if auth.enabled:
            raise HTTPException(status_code=403, detail="setup already complete")
        try:
            await asyncio.to_thread(
                auth.create_user, body.username.strip(), body.password, "super")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        if body.callsign.strip():
            db.set_setting("brand_callsign", body.callsign.strip())
        if body.node.strip():
            db.set_setting("brand_node", body.node.strip())
        if body.footer.strip():
            db.set_setting("footer_text", body.footer.strip())
        # log the new administrator straight in
        resp = JSONResponse({"ok": True, "username": body.username.strip()})
        resp.set_cookie(SESSION_COOKIE, auth.issue_token(body.username.strip()),
                        max_age=SESSION_TTL, httponly=True, samesite="lax",
                        secure=request.url.scheme == "https")
        return resp

    # ---- users (managing accounts is super-admin only) ----

    @app.get("/api/users")
    async def users(request: Request):
        require_settings(request)       # super/admin see the roster
        return {"users": await asyncio.to_thread(db.list_users)}

    @app.post("/api/users")
    async def add_user(body: UserBody, request: Request):
        require_super(request)
        role = body.role if body.role in ("super", "admin", "user") else "admin"
        try:
            await asyncio.to_thread(
                auth.create_user, body.username.strip(), body.password, role)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"ok": True}

    @app.post("/api/users/{username}/role")
    async def set_role(username: str, body: RoleBody, request: Request):
        require_super(request)
        try:
            await asyncio.to_thread(auth.set_role, username, body.role)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"ok": True}

    @app.delete("/api/users/{username}")
    async def remove_user(username: str, request: Request):
        acting = require_super(request)
        try:
            await asyncio.to_thread(auth.delete_user, username, acting)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"ok": True}

    # ---- status ----

    @app.get("/api/status")
    async def status(request: Request):
        link_ok = (time.time() - segmenter.last_frame_wall) < 120 \
            if segmenter.last_frame_wall else False
        user = current_user(request)
        return {
            # subtitle overrides; blank falls back to the configured values
            "brand_callsign": db.get_setting("brand_callsign"),
            "brand_node": db.get_setting("brand_node"),
            "node_number": cfg.node_number,
            "callsign": cfg.callsign,
            # header brand name (top-left) + optional multi-node subline
            "site_name": db.get_setting("site_name", cfg.site_name),
            "node_label": cfg.node_label,
            "version": __version__,
            "rx_active": segmenter.active or pipeline.rx_active,
            "link_ok": link_ok,
            "last_frame": segmenter.last_frame_wall or None,
            # the UDP port the node streams chan_usrp audio to — surfaced so the
            # UI can show the exact rxchannel target in the first-run node hint
            "usrp_port": cfg.usrp_port,
            "queue_depth": pipeline.queue_depth,
            "whisper_model": pipeline.whisper_model,
            "whisper_loading": pipeline.transcriber.loading_model,
            "whisper_available": pipeline.transcriber.available,
            "speaker_id_available": pipeline.speaker_id.available,
            "models": list(WHISPER_MODELS),
            "is_admin": user is not None,
            "is_super": auth.is_super(user),
            "can_settings": auth.can_settings(user),
            "username": user,
            # first run: no admin account yet -> the browser shows the setup wizard
            "needs_setup": not auth.enabled,
            # live connection count — a super/admin feature, so only
            # surfaced to settings-capable callers (the WS presence events
            # that keep it live are likewise sent to super/admin sockets only)
            **({"connections": broadcaster.client_count}
               if auth.can_settings(user) else {}),
            # the logged-in user's own profile-picture timestamp (for the
            # account tile); empty/absent means it shows initials
            "avatar_ts": (db.get_setting(f"avatar_ts:{user}") or None)
            if user else None,
            "retention_days": cfg.retention_days,
            # geolocation is admin-only, so don't even advertise it to others
            "has_geo": bool(cfg.voter_receivers) and user is not None,
            "theme": db.get_setting("theme", "paper"),
            "themes": list(THEMES),
            # live audio waterfall available (public — same audio anyone can hear)
            "has_bandscope": cfg.bandscope_enabled,
            # the Time Machine DVR (its voter/geo lanes stay login/admin-gated)
            "has_timemachine": cfg.timemachine_enabled,
            # server-side render of a scrubbed range (the render itself is
            # admin-gated; this flag just shows/hides the Export control)
            "has_export": cfg.export_enabled,
            # Say Again: cross-source callsign resolution + tap-to-loop (public)
            "has_sayagain": cfg.sayagain_enabled,
            # ElevenLabs on-demand "second opinion" reprocess (admin action)
            "has_elevenlabs": cfg.elevenlabs_enabled and elevenlabs_stt.available(),
            # stuck-repeater gate is up: transcription paused (public, like rx)
            "storm_active": pipeline.storm.active,
            "footer_text": db.get_setting("footer_text", cfg.footer_text),
            "logo_ts": db.get_setting("logo_ts"),
            # QRZ XML config state, for the settings panel (never the password)
            **({"qrz_username": db.get_setting("qrz_username", "")}
               if auth.can_settings(user) else {}),
            # voter polling state, for the settings panel (admin-only)
            **({"voter_idle_timeout": pipeline.voter.idle_enabled,
                "voter_idle_minutes": pipeline.voter.idle_minutes,
                "voter_polling_disabled": pipeline.voter.disabled,
                "has_voter": bool(cfg.voter_sources)}
               if auth.can_settings(user) else {}),
        }

    # ---- transmissions ----

    @app.get("/api/transmissions")
    async def transmissions(request: Request, limit: int = 50,
                            before_id: int | None = None,
                            q: str | None = None,
                            speaker_id: int | None = None,
                            origin: str | None = None,
                            mdc_unit: str | None = None,
                            since: float | None = None,
                            until: float | None = None,
                            has_mdc: bool = False,
                            unnamed: bool = False,
                            no_speech: bool = False):
        limit = max(1, min(limit, 200))
        rows = await asyncio.to_thread(
            db.list_transmissions, limit, before_id, q, speaker_id,
            origin, mdc_unit, since, until, has_mdc, unnamed, no_speech)
        if current_user(request) is None:
            # voter RSSI is login-gated, mirroring the geolocation it feeds
            for r in rows:
                r.pop("voter", None)
        return {"transmissions": rows}

    @app.get("/api/facets")
    async def facets():
        # values to populate the filter builder's dropdowns
        origins = await asyncio.to_thread(db.distinct_origins)
        speakers = await asyncio.to_thread(db.list_speakers)
        mdc_units = await asyncio.to_thread(db.distinct_mdc_units)
        return {
            "origins": origins,
            "mdc_units": mdc_units,
            "speakers": [{"id": s["id"], "label": s["label"],
                          "is_named": bool(s["is_named"])} for s in speakers],
        }

    @app.get("/api/stats")
    async def stats(days: int = 30, tz: int = 0, today: int = 0,
                    since: float | None = None, until: float | None = None):
        # public: an aggregate of the same activity the live feed already shows
        days = max(0, min(days, 3650))          # 0 = all time
        tz = max(-840, min(840, tz))            # sane UTC offset in minutes
        if since is not None or until is not None:
            pass                                # explicit window (e.g. a net)
        elif today:
            # since the viewer's local midnight (tz = minutes east of UTC)
            now = time.time()
            since = now - ((now + tz * 60) % 86400)
        else:
            since = time.time() - days * 86400 if days else None
        data = await asyncio.to_thread(db.activity_stats, since, until, tz)
        data["days"] = days
        return data

    @app.get("/api/callsign/{cs}")
    async def callsign_lookup(cs: str):
        # public: FCC license data is public record, keyed by heard callsigns
        cs = cs.strip().upper()
        if not CALL_RE.fullmatch(cs):
            raise HTTPException(status_code=400, detail="not a callsign")
        cached = await asyncio.to_thread(db.get_cached_callsign, cs)
        ttl = cfg.callbook_cache_days * 86400
        if cached and (cfg.callbook_cache_days <= 0
                       or time.time() - cached["fetched_at"] < ttl):
            result = cached["data"] or {"status": cached["status"]}
        elif not cfg.callbook_enabled:
            result = {"status": "disabled"}
        else:
            # QRZ (when configured) with FCC fallback — same path the
            # background enrichment worker uses
            result = await asyncio.to_thread(pipeline.lookup_callsign, cs)
            if result["status"] in ("found", "not_found"):   # don't cache errors
                await asyncio.to_thread(db.cache_callsign, cs, result,
                                        result["status"])
        speaker_id = await asyncio.to_thread(db.find_speaker_by_callsign, cs)
        return {"callsign": cs, "speaker_id": speaker_id, **result}

    @app.get("/api/callsigns")
    async def callsign_log():
        # public logbook: every callsign heard, with cached enrichment
        rows = await asyncio.to_thread(db.callsign_log)
        return {"callsigns": rows}

    @app.get("/api/avatars")
    async def avatars():
        # public: callsign -> QRZ photo URL, for the speaker avatars (the
        # photos themselves are public QRZ profile images)
        return {"images": await asyncio.to_thread(db.callsign_images)}

    @app.get("/api/connections")
    async def connections(request: Request, limit: int = 500,
                          before_id: int | None = None,
                          q: str | None = None, status: str = "all"):
        # the connection log carries visitor IPs — super/admin only, not
        # plain 'user' accounts
        require_settings(request)
        limit = max(1, min(limit, 1000))
        rows = await asyncio.to_thread(
            db.list_connections, limit, before_id, q, status)
        return {"connections": rows, "live": broadcaster.client_count}

    @app.get("/api/connections/stats")
    async def connections_stats(request: Request, days: int = 30, tz: int = 0,
                                since: float | None = None,
                                until: float | None = None):
        # visitor-IP analytics — super/admin only, same gate as the log
        require_settings(request)
        days = max(0, min(days, 3650))          # 0 = all time
        tz = max(-840, min(840, tz))
        if since is None and until is None:
            since = time.time() - days * 86400 if days else None
        data = await asyncio.to_thread(db.connection_stats, since, until, tz)
        data["live"] = broadcaster.client_count
        return data

    @app.get("/api/connections/export")
    async def connections_export(request: Request, fmt: str = "csv",
                                 q: str | None = None, status: str = "all"):
        require_settings(request)
        rows = await asyncio.to_thread(
            db.list_connections, 100_000, None, q, status)
        now = time.time()

        def ts(v):
            return (datetime.fromtimestamp(v).strftime("%Y-%m-%d %H:%M:%S")
                    if v else "")

        headers = ["IP", "User", "User Agent", "Joined", "Left", "Duration (s)"]
        table = []
        for c in rows:
            end = c["disconnected_at"]
            table.append([
                c["ip"] or "", c["username"] or "", c["user_agent"] or "",
                ts(c["connected_at"]),
                ts(end) if end else "active",
                int((end or now) - c["connected_at"]),
            ])
        stamp = datetime.fromtimestamp(now).strftime("%Y%m%d-%H%M")
        if fmt == "xlsx":
            data = tabexport.to_xlsx(headers, table, "Connections")
            return Response(
                content=data,
                media_type="application/vnd.openxmlformats-officedocument."
                           "spreadsheetml.sheet",
                headers={"Content-Disposition":
                         f'attachment; filename="squelch-connections-{stamp}.xlsx"'})
        return Response(
            content=tabexport.to_csv(headers, table),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition":
                     f'attachment; filename="squelch-connections-{stamp}.csv"'})

    @app.get("/api/transmissions/{tx_id}/similar")
    async def similar(tx_id: int, request: Request):
        rows = await asyncio.to_thread(db.similar_transmissions, tx_id, 20)
        # voter RSSI is login-gated everywhere (it feeds the admin-only
        # geolocation) — this listing must not leak it either
        if current_user(request) is None:
            for r in rows:
                r.pop("voter", None)
        return {"transmissions": rows}

    @app.get("/api/transmissions/{tx_id}/geo")
    async def tx_geo(tx_id: int, request: Request):
        require_admin(request)          # geolocation is admin-only
        if not cfg.voter_receivers:
            raise HTTPException(status_code=404, detail="no receiver coordinates configured")
        rec = await asyncio.to_thread(db.get_transmission, tx_id)
        if not rec or not rec.get("voter"):
            raise HTTPException(status_code=404, detail="no voter data for this transmission")
        track = geo.estimate_track(rec["voter"], cfg.voter_receivers)
        if track is None:
            raise HTTPException(status_code=404, detail="no located signal")
        return track

    @app.get("/api/transmissions/{tx_id}/audio")
    async def tx_audio(tx_id: int):
        path = await asyncio.to_thread(db.get_audio_path, tx_id)
        if not path or not Path(path).exists():
            raise HTTPException(status_code=404, detail="no audio")
        return FileResponse(path, media_type="audio/wav",
                            filename=f"tx_{tx_id}.wav")

    @app.get("/api/transmissions/{tx_id}/audio.mp3")
    async def tx_audio_mp3(tx_id: int, request: Request):
        require_admin(request)
        path = await asyncio.to_thread(db.get_audio_path, tx_id)
        if not path or not Path(path).exists():
            raise HTTPException(status_code=404, detail="no audio")
        if not shutil.which("ffmpeg"):
            raise HTTPException(
                status_code=501,
                detail="ffmpeg is not installed on the server "
                       "(apt install ffmpeg)")
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-v", "error", "-i", path,
            "-codec:a", "libmp3lame", "-qscale:a", "4", "-f", "mp3", "pipe:1",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        data, err = await proc.communicate()
        if proc.returncode != 0 or not data:
            log.error("ffmpeg failed for tx %d: %s", tx_id, err.decode())
            raise HTTPException(status_code=500, detail="mp3 conversion failed")
        return Response(
            content=data, media_type="audio/mpeg",
            headers={"Content-Disposition":
                     f'attachment; filename="tx_{tx_id}.mp3"'})

    @app.post("/api/timemachine/export")
    async def tm_export(body: TMExportBody, request: Request):
        require_admin(request)
        if not cfg.export_enabled:
            raise HTTPException(status_code=404, detail="export is disabled")
        if not shutil.which("ffmpeg"):
            raise HTTPException(
                status_code=501,
                detail="ffmpeg is not installed on the server "
                       "(apt install ffmpeg)")
        start, end = float(body.start), float(body.end)
        if not end > start:
            raise HTTPException(status_code=400, detail="empty time range")
        if end - start > cfg.export_max_span_secs:
            raise HTTPException(
                status_code=400,
                detail=f"range too long (max "
                       f"{int(cfg.export_max_span_secs / 60)} min)")
        job_id = exporter.submit(start, end, bool(body.captions))
        return {"job_id": job_id}

    @app.get("/api/timemachine/export/{job_id}")
    async def tm_export_status(job_id: str, request: Request):
        require_admin(request)
        st = exporter.status(job_id)
        if st is None:
            raise HTTPException(status_code=404, detail="no such export job")
        return st

    @app.get("/api/timemachine/export/{job_id}/file")
    async def tm_export_file(job_id: str, request: Request):
        require_admin(request)
        path = exporter.file_path(job_id)
        if not path:
            raise HTTPException(status_code=404, detail="export not ready")
        return FileResponse(
            path, media_type="video/mp4",
            filename=f"squelch-timemachine-{job_id}.mp4")

    @app.post("/api/transmissions/{tx_id}/reprocess")
    async def reprocess_transmission(tx_id: int, request: Request):
        require_admin(request)
        try:
            await pipeline.request_reprocess(tx_id)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"ok": True}

    @app.post("/api/transmissions/{tx_id}/second_opinion")
    async def second_opinion_transmission(tx_id: int, request: Request):
        # on-demand cloud re-transcribe of ONE over via ElevenLabs (admin only,
        # only when configured + a key is present)
        require_admin(request)
        if not (cfg.elevenlabs_enabled and elevenlabs_stt.available()):
            raise HTTPException(status_code=404, detail="ElevenLabs not configured")
        try:
            await pipeline.request_second_opinion(tx_id)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"ok": True}

    @app.delete("/api/transmissions/{tx_id}")
    async def delete_transmission(tx_id: int, request: Request):
        require_admin(request)
        audio_path = await asyncio.to_thread(db.delete_transmission, tx_id)
        if audio_path:
            delete_audio_files([audio_path])
        await broadcaster.send("tx_deleted", {"id": tx_id})
        return {"ok": True}

    @app.get("/api/transmissions/purge_count")
    async def purge_count(start: float, end: float, request: Request):
        require_admin(request)
        n = await asyncio.to_thread(
            db.count_transmissions_between, start, end)
        return {"count": n}

    # NOTE: registered after the literal /api/transmissions/... routes above —
    # a {tx_id:int} path would otherwise shadow them with 422s
    @app.get("/api/transmissions/{tx_id}")
    async def transmission(tx_id: int, request: Request):
        """Single transmission (public) — the permalink target."""
        rec = await asyncio.to_thread(db.get_transmission, tx_id)
        if rec is None:
            raise HTTPException(status_code=404, detail="no such transmission")
        if current_user(request) is None:
            rec.pop("voter", None)      # voter is login-gated (see /geo)
        return {"transmission": rec}

    @app.post("/api/transmissions/purge")
    async def purge(body: PurgeBody, request: Request):
        require_admin(request)
        if body.end <= body.start:
            raise HTTPException(status_code=400, detail="empty date range")
        n = await asyncio.to_thread(
            db.count_transmissions_between, body.start, body.end)
        paths = await asyncio.to_thread(
            db.delete_transmissions_between, body.start, body.end)
        delete_audio_files(paths)
        await broadcaster.send("feed_reload", {})
        log.info("purged %d transmissions (%d audio files)", n, len(paths))
        return {"ok": True, "deleted": n}

    @app.post("/api/transmissions/{tx_id}/speaker")
    async def assign_tx_speaker(tx_id: int, body: AssignBody, request: Request):
        require_admin(request)
        tx = await asyncio.to_thread(db.get_transmission, tx_id)
        if not tx:
            raise HTTPException(status_code=404, detail="no such transmission")

        speaker_id = body.speaker_id
        if speaker_id is None:
            label = (body.label or "").strip()
            if not label:
                raise HTTPException(status_code=400,
                                    detail="speaker_id or label required")
            speaker_id = await asyncio.to_thread(db.find_speaker_by_label, label)
            if speaker_id is None:
                speaker_id = await asyncio.to_thread(
                    db.create_speaker, label, None, True)

        # operator said so — the strongest provenance there is
        await asyncio.to_thread(
            db.assign_speaker, tx_id, speaker_id, None, "manual")
        # fold this voice into the speaker profile (verified enrollment)
        emb = await asyncio.to_thread(db.get_tx_embedding, tx_id)
        if emb is not None:
            await asyncio.to_thread(pipeline.speaker_id.enroll, speaker_id,
                                    emb, tx_id, True)

        record = await asyncio.to_thread(db.get_transmission, tx_id)
        await broadcaster.send("tx_update", {"tx": record})
        pipeline.revisit_soon()   # a verified sample landed — sweep unknowns
        return {"ok": True, "speaker_id": speaker_id}

    # ---- MDC ingest (from the node's forwarder) ----

    @app.post("/api/mdc")
    async def ingest_mdc(body: MDCBody, request: Request):
        if cfg.mdc_forward_token:
            token = request.headers.get("x-mdc-token", "")
            if token != cfg.mdc_forward_token:
                raise HTTPException(status_code=403, detail="bad token")
        from .mdc_ingest import make_entry
        entry = make_entry(body.type, body.unit, body.node)
        attached = await pipeline.mdc_matcher.ingest(entry, recv=body.ts)
        return {"ok": True, "attached": attached}

    @app.post("/api/source")
    async def ingest_source(body: SourceBody, request: Request):
        if cfg.mdc_forward_token:
            token = request.headers.get("x-mdc-token", "")
            if token != cfg.mdc_forward_token:
                raise HTTPException(status_code=403, detail="bad token")
        origin = "local" if body.source == "local" else (body.node or "").strip()
        if not origin:
            raise HTTPException(status_code=400, detail="missing node number")
        hub = (body.hub or "").strip() or None
        attached = await pipeline.source_matcher.ingest(origin, hub=hub, recv=body.ts)
        return {"ok": True, "attached": attached}

    @app.post("/api/link")
    async def ingest_link(body: LinkBody, request: Request):
        # link state from the Pi's source monitor, used to pause voter
        # polling when we're not connected (same LAN-trust token as /api/source)
        if cfg.mdc_forward_token:
            token = request.headers.get("x-mdc-token", "")
            if token != cfg.mdc_forward_token:
                raise HTTPException(status_code=403, detail="bad token")
        pipeline.voter.report_link(body.connected)
        pipeline.network.report(body.hub, body.connected)
        return {"ok": True, "gating": cfg.voter_gate_on_connect}

    @app.get("/api/link")
    async def link_status(request: Request):
        # voter gating state — for verifying link-gated polling. A browser
        # (Accept: text/html) gets a readable auto-refreshing status page;
        # everything else (and ?json) gets the raw JSON. An unauthorized
        # browser gets a friendly page instead of raw 403 JSON.
        user = current_user(request)
        wants_html = ("text/html" in request.headers.get("accept", "")
                      and "json" not in request.query_params)
        if not (user is not None and auth.can_settings(user)):
            if wants_html:
                return HTMLResponse(
                    _render_denied_page(), status_code=403,
                    headers={"Content-Security-Policy": _DENIED_CSP})
            require_settings(request)   # non-browser: the usual JSON 403
        snap = pipeline.voter.status_snapshot()
        if wants_html:
            return HTMLResponse(_render_voter_page(snap))
        return snap

    @app.get("/api/link.js")
    async def link_js():
        # the voter status page's checkbox wiring (loaded via <script src>).
        # Inert on its own; the endpoints it calls are require_settings-gated.
        return Response(_VOTER_PAGE_JS, media_type="text/javascript",
                        headers={"Cache-Control": "no-cache"})

    @app.post("/api/settings/voter_idle")
    async def set_voter_idle(body: VoterIdleBody, request: Request):
        require_settings(request)
        db.set_setting("voter_idle_timeout", "on" if body.enabled else "off")
        mins = None
        if body.minutes is not None:
            mins = max(1.0, min(float(body.minutes), 240.0))
            db.set_setting("voter_idle_minutes", str(mins))
        pipeline.voter.set_idle_timeout(body.enabled, mins)
        return {"ok": True, "idle_timeout": pipeline.voter.idle_enabled,
                "idle_minutes": pipeline.voter.idle_minutes}

    @app.post("/api/settings/voter_disabled")
    async def set_voter_disabled(body: VoterDisableBody, request: Request):
        require_settings(request)
        db.set_setting("voter_polling_disabled",
                       "on" if body.disabled else "off")
        pipeline.voter.set_disabled(body.disabled)
        return {"ok": True, "disabled": pipeline.voter.disabled}

    # ---- MDC unit -> operator mapping (voice-ID ground truth) ----

    @app.get("/api/mdc_units")
    async def mdc_units():
        # public: unit->operator labels shown on MDC badges for everyone
        return {"units": await asyncio.to_thread(db.list_mdc_operators)}

    @app.post("/api/mdc_units")
    async def link_mdc_unit(body: MDCUnitBody, request: Request):
        require_admin(request)
        unit = body.unit.strip()
        if not unit:
            raise HTTPException(status_code=400, detail="unit required")
        spk = await asyncio.to_thread(db.get_speaker, body.speaker_id)
        if spk is None:
            raise HTTPException(status_code=404, detail="no such speaker")
        await asyncio.to_thread(db.set_mdc_operator, unit, body.speaker_id)
        await broadcaster.send("mdc_units_changed", {})
        return {"ok": True, "unit": unit, "speaker_id": body.speaker_id,
                "label": spk["label"]}

    @app.delete("/api/mdc_units/{unit}")
    async def unlink_mdc_unit(unit: str, request: Request):
        require_admin(request)
        if not await asyncio.to_thread(db.delete_mdc_operator, unit):
            raise HTTPException(status_code=404, detail="unit not linked")
        await broadcaster.send("mdc_units_changed", {})
        return {"ok": True}

    # ---- node lookup (origin badge callsigns) ----

    @app.get("/api/nodes/{node}")
    async def node_info(node: str):
        info = nodedb.lookup(node)
        if info is None:
            raise HTTPException(status_code=404, detail="unknown node")
        return {"node": node, **info}

    @app.get("/api/network")
    async def network_status():
        # public: live per-hub link roster — the same node-connection data
        # AllStarLink already publishes at allstarlink.org/nodelist
        snap = pipeline.network.snapshot()
        hubset = {h["hub"] for h in snap}

        def _name(n: str) -> str | None:
            info = nodedb.lookup(n)
            return info.get("callsign") if info else None

        hubs = [{
            "hub": h["hub"],
            "name": _name(h["hub"]),
            "online": h["online"],
            "reported_at": h["reported_at"],
            "since": h["since"],
            "connected": [{"node": n, "name": _name(n), "is_hub": n in hubset}
                          for n in h["nodes"]],
        } for h in snap]
        return {"hubs": hubs, "generated_at": time.time()}

    # ---- speakers ----

    @app.get("/api/speakers")
    async def speakers():
        return {"speakers": await asyncio.to_thread(db.list_speakers)}

    @app.post("/api/speakers/reset_voiceprints")
    async def reset_voiceprints(request: Request):
        """Admin: wipe all learned voice samples and start identification
        fresh (named speakers and evidence-backed attributions survive)."""
        require_admin(request)
        counts = await asyncio.to_thread(db.reset_all_voiceprints)
        log.warning("all voiceprints reset by admin: %s", counts)
        await broadcaster.send("feed_reload", {})
        return {"ok": True, **counts}

    @app.get("/api/speakers/{speaker_id}")
    async def speaker_detail(speaker_id: int):
        stats = await asyncio.to_thread(db.speaker_stats, speaker_id)
        if stats is None:
            raise HTTPException(status_code=404, detail="no such speaker")
        return stats

    # ---- watchlist (admin) ----

    # alerts are personal: any logged-in user manages their OWN watches, and a
    # hit only ever notifies that user (see pipeline._run_watchlist)

    @app.get("/api/watchlist")
    async def get_watchlist(request: Request):
        user = require_admin(request)
        return {"watchlist": await asyncio.to_thread(db.list_watchlist, user)}

    @app.post("/api/watchlist")
    async def add_watch(body: WatchBody, request: Request):
        user = require_admin(request)
        kind = body.kind
        if kind not in ("callsign", "mdc_unit", "emergency", "speaker"):
            raise HTTPException(status_code=400, detail="unknown watch kind")
        value = body.value.strip()
        if kind != "emergency" and not value:
            raise HTTPException(status_code=400, detail="value required")
        webhook = body.webhook.strip()
        if webhook and urlparse(webhook).scheme not in ("http", "https"):
            raise HTTPException(status_code=400,
                                detail="webhook must be an http(s) URL")
        wid = await asyncio.to_thread(
            db.add_watch, user, kind, value, body.label.strip(), webhook)
        return {"ok": True, "id": wid}

    @app.delete("/api/watchlist/{watch_id}")
    async def del_watch(watch_id: int, request: Request):
        user = require_admin(request)
        if not await asyncio.to_thread(db.delete_watch, watch_id, user):
            raise HTTPException(status_code=404, detail="no such watch")
        return {"ok": True}

    @app.post("/api/watchlist/{watch_id}/enabled")
    async def set_watch_enabled(watch_id: int, body: WatchEnableBody,
                                request: Request):
        user = require_admin(request)
        if not await asyncio.to_thread(
                db.set_watch_enabled, watch_id, body.enabled, user):
            raise HTTPException(status_code=404, detail="no such watch")
        return {"ok": True}

    # ---- cases (investigative records; super/admin only) ----

    @app.get("/api/cases")
    async def list_cases_ep(request: Request, status: str | None = None):
        require_settings(request)
        return {"cases": await asyncio.to_thread(db.list_cases, status),
                "statuses": list(CASE_STATUSES)}

    @app.post("/api/cases")
    async def create_case_ep(body: CaseBody, request: Request):
        user = require_settings(request)
        title = body.title.strip()
        if not title:
            raise HTTPException(status_code=400, detail="title required")
        case = await asyncio.to_thread(
            db.create_case, title, user, body.subject.strip(), body.summary.strip())
        return {"ok": True, "case": case}

    @app.get("/api/cases/{case_id}")
    async def get_case_ep(case_id: int, request: Request):
        require_settings(request)
        case = await asyncio.to_thread(db.get_case, case_id)
        if not case:
            raise HTTPException(status_code=404, detail="no such case")
        return case

    @app.patch("/api/cases/{case_id}")
    async def update_case_ep(case_id: int, body: CaseUpdateBody, request: Request):
        user = require_settings(request)
        fields: dict = {}
        for k in ("title", "status", "subject", "summary"):
            v = getattr(body, k)
            if v is not None:
                fields[k] = v.strip() if isinstance(v, str) else v
        if not fields:
            raise HTTPException(status_code=400, detail="nothing to update")
        if fields.get("status") is not None and fields["status"] not in CASE_STATUSES:
            raise HTTPException(status_code=400, detail="invalid status")
        if "title" in fields and not fields["title"]:
            raise HTTPException(status_code=400, detail="title cannot be empty")
        if not await asyncio.to_thread(db.update_case, case_id, fields, user):
            raise HTTPException(status_code=404, detail="no such case")
        return {"ok": True, "case": await asyncio.to_thread(db.get_case, case_id)}

    @app.delete("/api/cases/{case_id}")
    async def delete_case_ep(case_id: int, request: Request):
        require_super(request)             # deleting a case file is super-admin only
        if not await asyncio.to_thread(db.delete_case, case_id):
            raise HTTPException(status_code=404, detail="no such case")
        return {"ok": True}

    @app.post("/api/cases/{case_id}/items")
    async def add_case_item_ep(case_id: int, body: CaseItemBody, request: Request):
        user = require_settings(request)
        r = await asyncio.to_thread(db.add_case_item, case_id, body.tx_id,
                                    body.label.strip(), body.note.strip(), user)
        if r is None:
            raise HTTPException(status_code=404,
                                detail="no such case or transmission")
        return {"ok": True, "already": r == 0,
                "case": await asyncio.to_thread(db.get_case, case_id)}

    @app.delete("/api/cases/{case_id}/items/{item_id}")
    async def remove_case_item_ep(case_id: int, item_id: int, request: Request):
        user = require_settings(request)
        if not await asyncio.to_thread(db.remove_case_item, case_id, item_id, user):
            raise HTTPException(status_code=404, detail="no such evidence item")
        return {"ok": True, "case": await asyncio.to_thread(db.get_case, case_id)}

    @app.post("/api/cases/{case_id}/notes")
    async def add_case_note_ep(case_id: int, body: CaseNoteBody, request: Request):
        user = require_settings(request)
        text = body.text.strip()
        if not text:
            raise HTTPException(status_code=400, detail="note text required")
        if await asyncio.to_thread(db.add_case_note, case_id, text, user) is None:
            raise HTTPException(status_code=404, detail="no such case")
        return {"ok": True, "case": await asyncio.to_thread(db.get_case, case_id)}

    @app.get("/api/cases/{case_id}/export")
    async def export_case_ep(case_id: int, request: Request):
        require_settings(request)
        case = await asyncio.to_thread(db.get_case, case_id)
        if not case:
            raise HTTPException(status_code=404, detail="no such case")
        site = db.get_setting("site_name", cfg.site_name) or "Squelch"
        return HTMLResponse(_render_case_report(case, site))

    # ---- web push (phone/desktop alerts when the app is closed) ----

    @app.get("/api/push/vapid")
    async def push_vapid(request: Request):
        require_admin(request)  # you must be logged in to receive alerts
        pub, _ = await asyncio.to_thread(webpush.get_or_create_keys, db)
        return {"key": pub, "enabled": webpush.available()}

    @app.post("/api/push/subscribe")
    async def push_subscribe(body: PushSubBody, request: Request):
        user = require_admin(request)
        ua = request.headers.get("user-agent", "")[:200]
        await asyncio.to_thread(
            db.add_push_subscription, body.endpoint,
            body.keys.p256dh, body.keys.auth, user, ua)
        return {"ok": True}

    @app.post("/api/push/unsubscribe")
    async def push_unsubscribe(body: PushUnsubBody, request: Request):
        require_admin(request)
        await asyncio.to_thread(db.delete_push_subscription, body.endpoint)
        return {"ok": True}

    @app.post("/api/speakers/{speaker_id}/rename")
    async def rename_speaker(speaker_id: int, body: LabelBody, request: Request):
        require_admin(request)
        label = body.label.strip()
        if not label:
            raise HTTPException(status_code=400, detail="empty label")
        # renaming to a label another speaker already has merges the two
        # (same person got split into multiple clusters)
        existing = await asyncio.to_thread(db.find_speaker_by_label, label)
        if existing is not None and existing != speaker_id:
            await asyncio.to_thread(db.merge_speakers, speaker_id, existing)
            await broadcaster.send("feed_reload", {})
            pipeline.revisit_soon()   # merged profile may resolve old unknowns
            return {"ok": True, "merged_into": existing}
        ok = await asyncio.to_thread(db.rename_speaker, speaker_id, label)
        if not ok:
            raise HTTPException(status_code=404, detail="no such speaker")
        await broadcaster.send("speaker_renamed",
                               {"speaker_id": speaker_id, "label": label})
        pipeline.revisit_soon()       # named identity — sweep old unknowns
        return {"ok": True}

    @app.post("/api/speakers/{speaker_id}/rebuild")
    async def rebuild_voiceprint(speaker_id: int, request: Request):
        # recompute the voiceprint from this speaker's assigned transmissions,
        # rejecting outliers — cleanup after a mis-enrollment
        require_admin(request)
        n = await asyncio.to_thread(db.rebuild_voiceprint, speaker_id)
        return {"ok": True, "samples": n}

    @app.post("/api/speakers/{speaker_id}/reset")
    async def reset_voiceprint(speaker_id: int, request: Request):
        # wipe the voice profile entirely so it re-learns from scratch — for a
        # print too far gone to salvage by rebuild
        require_admin(request)
        await asyncio.to_thread(db.clear_speaker_profile, speaker_id)
        await broadcaster.send("feed_reload", {})
        return {"ok": True}

    # ---- branding: logo ----

    def _logo_path() -> Path | None:
        ext = db.get_setting("logo_ext")
        if not ext:
            return None
        path = branding_dir / f"logo{ext}"
        return path if path.exists() else None

    @app.get("/api/logo")
    async def get_logo():
        path = _logo_path()
        if path is None:
            raise HTTPException(status_code=404, detail="no custom logo")
        media = {v: k for k, v in LOGO_TYPES.items()}[path.suffix]
        # sandbox neutralizes scripts if an SVG is opened directly
        return FileResponse(path, media_type=media, headers={
            "Content-Security-Policy": "sandbox",
            "Cache-Control": "no-cache"})

    @app.post("/api/settings/logo")
    async def set_logo(file: UploadFile, request: Request):
        require_settings(request)
        ext = LOGO_TYPES.get(file.content_type or "")
        if ext is None:
            raise HTTPException(
                status_code=400,
                detail="logo must be PNG, JPEG, SVG, or WebP")
        data = await file.read()
        if len(data) > LOGO_MAX_BYTES:
            raise HTTPException(status_code=400,
                                detail="logo too large (512 KB max)")
        branding_dir.mkdir(parents=True, exist_ok=True)
        old = _logo_path()
        (branding_dir / f"logo{ext}").write_bytes(data)
        if old is not None and old.suffix != ext:
            old.unlink(missing_ok=True)
        ts = str(int(time.time()))
        db.set_setting("logo_ext", ext)
        db.set_setting("logo_ts", ts)
        await broadcaster.send("branding_changed", {"logo_ts": ts})
        return {"ok": True}

    @app.delete("/api/settings/logo")
    async def reset_logo(request: Request):
        require_settings(request)
        path = _logo_path()
        if path is not None:
            path.unlink(missing_ok=True)
        db.set_setting("logo_ext", "")
        db.set_setting("logo_ts", "")
        await broadcaster.send("branding_changed", {"logo_ts": None})
        return {"ok": True}

    # ---- account: per-user profile picture ----

    def _avatar_path(username: str) -> Path | None:
        ext = db.get_setting(f"avatar_ext:{username}")
        if not ext:
            return None
        path = avatar_dir / f"{username}{ext}"
        return path if path.exists() else None

    @app.get("/api/account/avatar")
    async def get_avatar(request: Request):
        # a logged-in account's own picture (used by the header tile)
        user = require_admin(request)
        path = _avatar_path(user)
        if path is None:
            raise HTTPException(status_code=404, detail="no profile picture")
        media = {v: k for k, v in AVATAR_TYPES.items()}[path.suffix]
        return FileResponse(path, media_type=media,
                            headers={"Cache-Control": "no-cache"})

    @app.post("/api/account/avatar")
    async def set_avatar(file: UploadFile, request: Request):
        user = require_admin(request)
        ext = AVATAR_TYPES.get(file.content_type or "")
        if ext is None:
            raise HTTPException(
                status_code=400,
                detail="profile picture must be PNG, JPEG, or WebP")
        data = await file.read()
        if len(data) > AVATAR_MAX_BYTES:
            raise HTTPException(status_code=400,
                                detail="picture too large (1 MB max)")
        avatar_dir.mkdir(parents=True, exist_ok=True)
        old = _avatar_path(user)
        (avatar_dir / f"{user}{ext}").write_bytes(data)
        if old is not None and old.suffix != ext:
            old.unlink(missing_ok=True)
        ts = str(int(time.time()))
        db.set_setting(f"avatar_ext:{user}", ext)
        db.set_setting(f"avatar_ts:{user}", ts)
        return {"ok": True, "ts": ts}

    @app.delete("/api/account/avatar")
    async def delete_avatar(request: Request):
        user = require_admin(request)
        path = _avatar_path(user)
        if path is not None:
            path.unlink(missing_ok=True)
        db.set_setting(f"avatar_ext:{user}", "")
        db.set_setting(f"avatar_ts:{user}", "")
        return {"ok": True}

    # ---- settings ----

    @app.post("/api/settings/whisper_model")
    async def set_model(body: ModelBody, request: Request):
        require_settings(request)
        try:
            pipeline.set_whisper_model(body.model)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        await broadcaster.send("model_changed", {"model": body.model})
        return {"ok": True}

    @app.post("/api/settings/theme")
    async def set_theme(body: ThemeBody, request: Request):
        require_settings(request)
        if body.theme not in THEMES:
            raise HTTPException(status_code=400, detail="unknown theme")
        db.set_setting("theme", body.theme)
        await broadcaster.send("theme_changed", {"theme": body.theme})
        return {"ok": True}

    @app.post("/api/settings/footer")
    async def set_footer(body: TextBody, request: Request):
        require_settings(request)
        text = body.text.strip()
        db.set_setting("footer_text", text)
        await broadcaster.send("footer_changed", {"text": text})
        return {"ok": True}

    @app.post("/api/settings/qrz")
    async def set_qrz(body: QrzBody, request: Request):
        # QRZ XML Data credentials (the user's qrz.com LOGIN — the XML API
        # trades it for a session key; requires an XML/premium subscription).
        # Blank both to disable and fall back to the free FCC feed.
        require_settings(request)
        user, pw = body.username.strip(), body.password
        changed = (user != db.get_setting("qrz_username", "")
                   or (pw != "" and pw != db.get_setting("qrz_password", "")))
        db.set_setting("qrz_username", user)
        if pw or not user:              # keep the stored password when only
            db.set_setting("qrz_password", pw)   # re-saving the username
        qrz.reset_session()
        requeued = 0
        if changed and user and db.get_setting("qrz_password", ""):
            # refresh existing entries so they pick up email/photo
            await asyncio.to_thread(db.clear_callsign_cache)
            requeued = await asyncio.to_thread(pipeline.requeue_callsigns)
        return {"ok": True, "requeued": requeued}

    @app.post("/api/settings/branding")
    async def set_branding(body: BrandBody, request: Request):
        # the subtitle under "Squelch": a callsign and a node number, each
        # blank = fall back to the node's configured value
        require_settings(request)
        cs, node = body.callsign.strip(), body.node.strip()
        db.set_setting("brand_callsign", cs)
        db.set_setting("brand_node", node)
        await broadcaster.send("subline_changed", {"callsign": cs, "node": node})
        return {"ok": True}

    # ---- websocket ----

    @app.websocket("/ws")
    async def websocket(ws: WebSocket):
        # the live feed is public, but voter RSSI (admin-only, feeds geo) is
        # stripped for unauthenticated sockets by the broadcaster
        user = auth.verify_token(ws.cookies.get(SESSION_COOKIE))
        await ws.accept()
        broadcaster.add(ws, is_admin=user is not None, username=user,
                        can_settings=auth.can_settings(user))
        # record the connection for the log + bump everyone's live counter
        conn_id = None
        try:
            conn_id = await asyncio.to_thread(
                db.connection_open, _peer_ip(ws.headers, ws.client), user,
                ws.headers.get("user-agent"), time.time())
        except Exception:
            log.exception("failed to log connection open")
        await broadcaster.broadcast_presence()
        try:
            while True:
                await ws.receive_text()  # ignore client chatter / pings
        except WebSocketDisconnect:
            pass
        finally:
            broadcaster.remove(ws)
            if conn_id is not None:
                try:
                    await asyncio.to_thread(
                        db.connection_close, conn_id, time.time())
                except Exception:
                    log.exception("failed to log connection close")
            await broadcaster.broadcast_presence()

    @app.websocket("/ws/audio")
    async def audio_stream(ws: WebSocket):
        """Binary PCM stream for the Listen Live feature (public — no auth)."""
        await ws.accept()
        live_audio.add(ws)
        try:
            while True:
                await ws.receive_bytes()  # keepalive; client sends nothing meaningful
        except WebSocketDisconnect:
            pass
        finally:
            live_audio.remove(ws)

    # hashed Next.js assets (JS/CSS chunks) referenced as /_next/... by the
    # exported frontend. Mounted only when a build is present.
    if (WEB_OUT_DIR / "_next").is_dir():
        app.mount("/_next", StaticFiles(directory=WEB_OUT_DIR / "_next"), name="next")
    if (WEB_OUT_DIR / "icons").is_dir():
        app.mount("/icons", StaticFiles(directory=WEB_OUT_DIR / "icons"), name="icons")
    if (WEB_OUT_DIR / "fonts").is_dir():
        app.mount("/fonts", StaticFiles(directory=WEB_OUT_DIR / "fonts"), name="fonts")
    return app
