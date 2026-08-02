"use client";

import { useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import { speakerHue } from "@/lib/format";
import type { GeoData, Transmission } from "@/lib/types";
import { useApp } from "@/state/app-context";

/**
 * Time Machine — the day on one clock.
 *
 * A fullscreen DVR deck fed by the REAL overs in a window. One authoritative
 * virtual clock `T` (epoch seconds) drives every lane each frame, so nothing
 * can drift — every view is a pure function of T.
 *
 *  Phase 1  clock + tape/overs/cues/talkers lanes + karaoke teleprompter
 *  Phase 2  audio as a SLAVE (seeked to follow T, never read back)
 *  Phase 3  instrument lanes — voter vote-handoff, signal quality, and a
 *           fuzzy-geo inset (all login/receiver gated, matching the app)
 *  Phase 4  scale & navigation — an overview ribbon + a zoomable detail
 *           viewport, text search, and solo-an-operator
 *  Phase 5  server-side export of the current view (MP4 + burned captions)
 *
 * Rendering is imperative (canvas + refs) because the clock updates ~60fps;
 * React only mounts the shell and hands the real overs to `buildDeck`.
 */

const WCV = 1200; // logical canvas width; CSS scales it, DOM overlays use %
const LAB = 76; // label column width, matches CSS
const EXPORT_MAX = 1800; // client hint; server enforces its own cap

const VOTE_PAL = [
  "#6ea8fe", "#63e6be", "#ffd43b", "#ff8787", "#da77f2",
  "#4dd4ac", "#ffa94d", "#9775fa", "#ff922b", "#66d9e8",
];
const QCOL: Record<string, string> = {
  excellent: "#51cf66", good: "#94d82d", fair: "#ffd43b",
  noisy: "#ffa94d", flutter: "#da77f2", clipping: "#ff6b6b",
};

interface VNode { node: string; info?: string; clients: string[]; samples: [number, number[], number][]; }

interface Over {
  id: number; hasAudio: boolean;
  start: number; end: number; dur: number;
  sp: string; spid: number | null; call: string; hue: number; key: string;
  snr: number | null; q: string; clip: boolean; flutter: number;
  words: [string, number, number][]; text: string;
  peaks: number[];
  dtmf: { d: string; t: number }[];
  mdc: string[];
  verified: string | null;
  voter: VNode[] | null;
}

function toOver(t: Transmission): Over {
  const label = t.speaker_id != null ? (t.speaker_label || `Speaker ${t.speaker_id}`) : "unknown";
  const call = (t.callsigns && t.callsigns[0]) || (t.speaker_id != null ? label : "");
  const end = t.ended_at ?? t.started_at + (t.duration_ms || 300) / 1000;
  const words = (t.words || []) as [string, number, number][];
  const call2 = call || "· · ·";
  return {
    id: t.id, hasAudio: !!t.has_audio,
    start: t.started_at, end, dur: Math.max(0.3, end - t.started_at),
    sp: label, spid: t.speaker_id ?? null, call: call2, hue: speakerHue(label),
    key: t.speaker_id != null ? "s" + t.speaker_id : label,
    snr: t.quality?.snr ?? null, q: t.quality?.label || "",
    clip: !!t.quality?.clipping, flutter: t.quality?.flutter ?? 0,
    words, text: (words.map((w) => w[0]).join(" ") + " " + call2).toLowerCase(),
    peaks: t.peaks || [],
    dtmf: (t.dtmf || []).map((p) => ({ d: p.d, t: p.t })),
    mdc: (t.mdc || []).map((m) => (m.unit_raw ?? m.unit_id_hex ?? m.label) || "MDC"),
    verified: t.speaker_verified ?? null,
    voter: (t.voter?.nodes as VNode[] | undefined) ?? null,
  };
}

interface Ctx { loggedIn: boolean; hasGeo: boolean; hasExport: boolean; }

export function TimeMachine() {
  const { status, closeModal } = useApp();
  const [phase, setPhase] = useState<"loading" | "empty" | "ready" | "error">("loading");

  // refs into the shell
  const stackRef = useRef<HTMLDivElement>(null);
  const ribRef = useRef<HTMLDivElement>(null);
  const waveRef = useRef<HTMLCanvasElement>(null);
  const ribCvRef = useRef<HTMLCanvasElement>(null);
  const wordsRef = useRef<HTMLDivElement>(null);
  const mkRef = useRef<HTMLDivElement>(null);
  const rostRef = useRef<HTMLDivElement>(null);
  const sigRef = useRef<HTMLCanvasElement>(null);
  const voteRef = useRef<HTMLCanvasElement>(null);
  const voteLaneRef = useRef<HTMLDivElement>(null);
  const headRef = useRef<HTMLDivElement>(null);
  const shadeRef = useRef<HTMLDivElement>(null);
  const tcRef = useRef<HTMLSpanElement>(null);
  const tcSubRef = useRef<HTMLSpanElement>(null);
  const whoRef = useRef<HTMLDivElement>(null);
  const capRef = useRef<HTMLDivElement>(null);
  const legendRef = useRef<HTMLDivElement>(null);
  const geoRef = useRef<HTMLCanvasElement>(null);
  const geoWrapRef = useRef<HTMLDivElement>(null);
  const playRef = useRef<HTMLButtonElement>(null);
  const teardownRef = useRef<() => void>(() => {});

  const loggedIn = !!status?.is_admin;
  const hasGeo = !!status?.has_geo;
  const hasExport = !!status?.has_export;

  useEffect(() => {
    let alive = true;
    (async () => {
      let overs: Over[] = [];
      try {
        const now = Date.now() / 1000;
        let res = await api.transmissions({ since: now - 7200, until: now + 120, limit: 200 });
        if (res.transmissions.length < 3) res = await api.transmissions({ limit: 60 });
        overs = res.transmissions.map(toOver).sort((a, b) => a.start - b.start);
      } catch {
        if (alive) setPhase("error");
        return;
      }
      if (!alive) return;
      if (!overs.length) { setPhase("empty"); return; }
      setPhase("ready");
      teardownRef.current = buildDeck(overs, {
        stack: stackRef.current!, rib: ribRef.current!, wave: waveRef.current!,
        ribCv: ribCvRef.current!, words: wordsRef.current!, mk: mkRef.current!,
        rost: rostRef.current!, sig: sigRef.current!, vote: voteRef.current!,
        voteLane: voteLaneRef.current!, head: headRef.current!, shade: shadeRef.current!,
        tc: tcRef.current!, tcSub: tcSubRef.current!, who: whoRef.current!,
        cap: capRef.current!, legend: legendRef.current!, geo: geoRef.current!,
        geoWrap: geoWrapRef.current!, play: playRef.current!,
      }, { loggedIn, hasGeo, hasExport });
    })();
    return () => { alive = false; teardownRef.current(); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const h = (e: KeyboardEvent) => { if (e.key === "Escape") closeModal(); };
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [closeModal]);

  return (
    <div className="tm-overlay" role="dialog" aria-label="Time Machine">
      <div className="tm-deck">
        <div className="tm-transport">
          <span className="tm-title">◷ TIME MACHINE</span>
          <button className="tm-btn play" ref={playRef}>▶ Play</button>
          <span className="tm-speed">
            <button className="tm-btn spd on" data-spd="1">1×</button>
            <button className="tm-btn spd" data-spd="4">4×</button>
          </span>
          <span className="tm-zoom">
            <button className="tm-btn" data-zoom="out" title="Zoom out">－</button>
            <button className="tm-btn" data-zoom="in" title="Zoom in">＋</button>
            <button className="tm-btn" data-zoom="fit" title="Fit whole timeline">⤢ Fit</button>
          </span>
          <span className="tm-find">
            <input className="tm-search" data-search placeholder="find callsign / word…" spellCheck={false} />
            <span className="tm-searchn" data-searchn></span>
          </span>
          <button className="tm-btn" data-ripple title="Collapse the dead air between overs">⤺ Compress silence</button>
          <button className="tm-btn" data-mute title="Audio plays at 1× · Play to hear it">🔊 Audio</button>
          <button className="tm-btn tm-solo-chip" data-solochip hidden></button>
          {hasExport && loggedIn && (
            <button className="tm-btn" data-export title="Render this view to an MP4">⤓ Export</button>
          )}
          <span className="tm-tc" ref={tcRef}>--:--:--<span className="sub" ref={tcSubRef} /></span>
          <button className="tm-btn tm-close" onClick={closeModal} aria-label="Close">✕ Close</button>
        </div>

        <div className="tm-export-panel" data-exportpanel hidden>
          <span className="tm-exp-range" data-exprange></span>
          <label className="tm-exp-cap"><input type="checkbox" data-expcap defaultChecked /> burn captions</label>
          <button className="tm-btn play" data-exprender>Render</button>
          <span className="tm-exp-prog" data-expprog hidden><span className="bar" data-expbar></span></span>
          <span className="tm-exp-msg" data-expmsg></span>
          <a className="tm-btn" data-expdl hidden download>⤓ Download</a>
        </div>

        <div className="tm-ribbon" ref={ribRef}>
          <canvas ref={ribCvRef} />
          <div className="tm-vrect" data-vrect />
          <span className="tm-riblabel" data-riblabel />
        </div>

        <div className="tm-stack" ref={stackRef}>
          <div className="tm-lane"><div className="lab">Tape</div><div className="body"><canvas ref={waveRef} /></div></div>
          <div className="tm-lane"><div className="lab">Overs</div><div className="body" ref={wordsRef} style={{ height: 24 }} /></div>
          <div className="tm-lane"><div className="lab">Signal</div><div className="body"><canvas ref={sigRef} /></div></div>
          <div className="tm-lane" ref={voteLaneRef}><div className="lab">Vote</div><div className="body"><canvas ref={voteRef} /></div></div>
          <div className="tm-lane"><div className="lab">Cues</div><div className="body" ref={mkRef} style={{ height: 22 }} /></div>
          <div className="tm-lane"><div className="lab">Talkers</div><div className="body" ref={rostRef} /></div>
          <div className="tm-shade" ref={shadeRef} />
          <div className="tm-head" ref={headRef} />
        </div>

        <div className="tm-legend" ref={legendRef} />

        <div className="tm-tele">
          <div className="tm-tele-main">
            <div className="tm-who" ref={whoRef} />
            <div className="tm-cap" ref={capRef} />
          </div>
          <div className="tm-geo" ref={geoWrapRef} hidden>
            <canvas ref={geoRef} width={176} height={120} />
            <span className="tm-geo-note">≈ fuzzy location · not DF</span>
          </div>
        </div>
      </div>

      {phase !== "ready" && (
        <div className="tm-status">
          {phase === "loading" && <span>Loading the timeline…</span>}
          {phase === "empty" && <span>No recent traffic to scrub yet.</span>}
          {phase === "error" && <span>Couldn&apos;t load the timeline.</span>}
        </div>
      )}
    </div>
  );
}

// ---- imperative deck ----
interface Els {
  stack: HTMLDivElement; rib: HTMLDivElement; wave: HTMLCanvasElement; ribCv: HTMLCanvasElement;
  words: HTMLDivElement; mk: HTMLDivElement; rost: HTMLDivElement; sig: HTMLCanvasElement;
  vote: HTMLCanvasElement; voteLane: HTMLDivElement; head: HTMLDivElement; shade: HTMLDivElement;
  tc: HTMLSpanElement; tcSub: HTMLSpanElement; who: HTMLDivElement; cap: HTMLDivElement;
  legend: HTMLDivElement; geo: HTMLCanvasElement; geoWrap: HTMLDivElement; play: HTMLButtonElement;
}
interface Seg { a: number; b: number; name: string | null; }

function buildDeck(overs: Over[], el: Els, ctx: Ctx): () => void {
  const clamp = (v: number, a: number, b: number) => (v < a ? a : v > b ? b : v);
  const T0 = overs[0].start - 4;
  const T1 = overs[overs.length - 1].end + 4;
  const SPAN = Math.max(1, T1 - T0);
  const MINSPAN = Math.min(SPAN, 8);
  // viewport (detail) — a window [V0,V1] within the full span [T0,T1]
  let V0 = T0, V1 = T1;
  const vspan = () => V1 - V0;
  const vfrac = (t: number) => (t - V0) / (V1 - V0);
  const pct = (t: number) => vfrac(t) * 100;
  const overAt = (t: number) => { for (const o of overs) if (t >= o.start && t <= o.end) return o; return null; };
  const css = getComputedStyle(document.documentElement);
  const cvar = (n: string) => css.getPropertyValue(n).trim() || "#888";
  const accent = cvar("--accent"), line = cvar("--border-soft");
  const spCol = (o: Over) => `hsl(${o.hue}, 62%, 62%)`;

  // ---- voter client palette (stable across the whole window) ----
  const clientColor = new Map<string, string>();
  for (const o of overs) for (const nd of (o.voter || [])) for (const c of nd.clients)
    if (!clientColor.has(c)) clientColor.set(c, VOTE_PAL[clientColor.size % VOTE_PAL.length]);
  const hasVoter = ctx.loggedIn && clientColor.size > 0;

  // per-over vote-handoff segments (winner runs), computed once
  const segMap = new Map<number, Seg[]>();
  function voteSegs(o: Over): Seg[] {
    let segs = segMap.get(o.id);
    if (segs) return segs;
    segs = [];
    for (const nd of (o.voter || [])) {
      const s = nd.samples;
      for (let i = 0; i < s.length; i++) {
        const tr = s[i][0], rssi = s[i][1], mask = s[i][2];
        const nextT = i + 1 < s.length ? s[i + 1][0] : o.dur;
        let win = -1, best = -1;
        for (let b = 0; b < nd.clients.length; b++) {
          if ((mask >> b) & 1) { const r = rssi[b] || 0; if (r > best) { best = r; win = b; } }
        }
        segs.push({ a: o.start + tr, b: o.start + Math.max(tr, nextT), name: win >= 0 ? nd.clients[win] : null });
      }
    }
    segMap.set(o.id, segs);
    return segs;
  }
  const winnerAt = (o: Over, t: number): string | null => {
    for (const s of voteSegs(o)) if (t >= s.a && t < s.b) return s.name;
    return null;
  };

  // ---- state ----
  let ripple = false, playing = false, spd = 1, T = overs[0].start, last = 0, raf = 0;
  let soloKey: string | null = null;
  let matchIds = new Set<number>();
  let matchList: Over[] = [];
  const passesSolo = (o: Over) => !soloKey || o.key === soloKey;
  const dimOf = (o: Over) => (passesSolo(o) ? 1 : 0.18);

  const cleanups: Array<() => void> = [];
  const on = <K extends keyof HTMLElementEventMap>(node: HTMLElement, ev: K,
    fn: (e: HTMLElementEventMap[K]) => void, opts?: AddEventListenerOptions) => {
    node.addEventListener(ev, fn as EventListener, opts);
    cleanups.push(() => node.removeEventListener(ev, fn as EventListener, opts));
  };

  // ---- canvas helpers ----
  function fit(cv: HTMLCanvasElement, h: number) { cv.width = WCV; cv.height = h; cv.style.height = h + "px"; }
  const xOf = (t: number) => vfrac(t) * WCV;

  function drawWave() {
    const cv = el.wave, h = 54; fit(cv, h); const c = cv.getContext("2d")!; c.clearRect(0, 0, WCV, h);
    const mid = h / 2; c.strokeStyle = line; c.beginPath(); c.moveTo(0, mid); c.lineTo(WCV, mid); c.stroke();
    for (const o of overs) {
      const x0 = xOf(o.start), x1 = xOf(o.end), w = x1 - x0;
      if (x1 < 0 || x0 > WCV || w <= 0) continue;
      const pk = o.peaks.length ? o.peaks : [0.2];
      c.globalAlpha = dimOf(o);
      c.fillStyle = "rgba(160,170,185,.55)";
      const n = Math.max(2, Math.min(pk.length, Math.round(w)));
      for (let i = 0; i < n; i++) { const v = pk[Math.floor(i / n * pk.length)] || 0; const x = x0 + w * i / n; const a = Math.max(1, v * (mid - 2)); c.fillRect(x, mid - a, Math.max(1, w / n - 0.3), a * 2); }
    }
    c.globalAlpha = 1;
  }

  function drawSignal() {
    const cv = el.sig, h = 26; fit(cv, h); const c = cv.getContext("2d")!; c.clearRect(0, 0, WCV, h);
    for (const o of overs) {
      const x0 = xOf(o.start), x1 = xOf(o.end), w = Math.max(1.5, x1 - x0);
      if (x1 < 0 || x0 > WCV) continue;
      const snr = o.snr ?? 12;
      const bh = 4 + Math.min(1, snr / 40) * (h - 7);
      c.globalAlpha = dimOf(o);
      c.fillStyle = QCOL[o.q] || "#8a97a8";
      c.fillRect(x0, h - bh, w, bh);
      if (o.clip) { c.fillStyle = "#ff6b6b"; c.fillRect(x0, 0, w, 2.5); }
      c.globalAlpha = 1;
    }
  }

  function drawVote() {
    const cv = el.vote, h = 20; fit(cv, h); const c = cv.getContext("2d")!; c.clearRect(0, 0, WCV, h);
    if (!hasVoter) return;
    for (const o of overs) {
      if (!o.voter) continue;
      if (xOf(o.end) < 0 || xOf(o.start) > WCV) continue;
      const a = dimOf(o);
      for (const s of voteSegs(o)) {
        const x0 = xOf(s.a), x1 = xOf(s.b), w = x1 - x0;
        if (x1 < 0 || x0 > WCV || w <= 0) continue;
        c.globalAlpha = a * (s.name ? 0.9 : 0.25);
        c.fillStyle = s.name ? (clientColor.get(s.name) || "#888") : "#5a6472";
        c.fillRect(x0, 3, Math.max(0.6, w), h - 6);
      }
    }
    c.globalAlpha = 1;
  }

  // ---- overview ribbon (full span; density + match/solo aware) ----
  function drawRibbon() {
    const cv = el.ribCv, W = WCV, H = 34; fit(cv, H); cv.style.width = "100%"; cv.style.height = H + "px";
    const c = cv.getContext("2d")!; c.clearRect(0, 0, W, H);
    const f0 = (t: number) => (t - T0) / SPAN * W;
    // density heat behind the overs
    const BK = 120; const dens = new Array(BK).fill(0);
    for (const o of overs) { const b = Math.min(BK - 1, Math.floor(((o.start - T0) / SPAN) * BK)); dens[b]++; }
    const dmax = Math.max(1, ...dens);
    for (let b = 0; b < BK; b++) { if (!dens[b]) continue; c.globalAlpha = 0.06 + 0.12 * (dens[b] / dmax); c.fillStyle = accent; c.fillRect(b / BK * W, 0, W / BK + 1, H); }
    c.globalAlpha = 1;
    for (const o of overs) {
      const x0 = f0(o.start), x1 = f0(o.end), col = spCol(o);
      c.globalAlpha = passesSolo(o) ? 1 : 0.25;
      c.fillStyle = col; c.globalAlpha *= 0.5; c.fillRect(x0, 9, Math.max(2, x1 - x0), H - 9);
      c.globalAlpha = passesSolo(o) ? 1 : 0.25; c.fillStyle = col; c.fillRect(x0, 3, Math.max(2, x1 - x0), 3);
      if (matchIds.has(o.id)) { c.globalAlpha = 1; c.fillStyle = "#fff"; c.fillRect(x0 - 1, 0, Math.max(2, x1 - x0) + 2, 2); }
    }
    c.globalAlpha = 1; c.fillStyle = accent;
    for (const o of overs) { o.dtmf.forEach((p) => { const x = f0(o.start + p.t); c.fillRect(x - 1, H - 3, 2, 3); }); if (o.mdc.length) { const x = f0(o.start); c.fillRect(x - 1, H - 3, 2, 3); } }
  }

  // ---- DOM overlays (labels/cues/talkers) ----
  interface OvEl { o: Over; el: HTMLElement; }
  let ovEls: OvEl[] = [], mkEls: { t: number; el: HTMLElement }[] = [], rbEls: OvEl[] = [];
  function buildOverlays() {
    el.words.innerHTML = ""; ovEls = [];
    for (const o of overs) { const d = document.createElement("div"); d.className = "tm-ovlab"; d.textContent = o.call; d.style.color = spCol(o); el.words.appendChild(d); ovEls.push({ o, el: d }); }
    el.mk.innerHTML = ""; mkEls = [];
    const addMk = (t: number, g: string) => { const s = document.createElement("div"); s.className = "tm-mk"; s.innerHTML = '<span class="g">' + escapeHtml(g) + "</span>"; el.mk.appendChild(s); mkEls.push({ t, el: s }); };
    for (const o of overs) {
      if (o.dtmf.length) addMk(o.start + o.dtmf[0].t, "⌗ " + o.dtmf.map((x) => x.d).join(""));
      if (o.mdc.length) addMk(o.start + 0.05, "◆ " + o.mdc[0]);
    }
    // Talkers: one row per speaker, a SEGMENT PER ACTUAL OVER (not one bar
    // from first-heard to last-heard). The row name toggles solo.
    const by: Record<string, { key: string; call: string; hue: number; ovs: Over[] }> = {};
    for (const o of overs) (by[o.key] || (by[o.key] = { key: o.key, call: o.call, hue: o.hue, ovs: [] })).ovs.push(o);
    const rows = Object.values(by); const rowH = 16, bh = Math.max(46, rows.length * rowH);
    el.rost.style.height = bh + "px"; el.rost.innerHTML = ""; rbEls = [];
    rows.forEach((p, i) => {
      const cy = i * rowH + rowH / 2, col = `hsl(${p.hue},62%,60%)`;
      for (const o of p.ovs) {
        const b = document.createElement("div"); b.className = "tm-rblock";
        b.style.top = cy + "px"; b.style.background = col; b.style.color = col; el.rost.appendChild(b); rbEls.push({ o, el: b });
      }
      const nm = document.createElement("div"); nm.className = "tm-rname"; nm.style.top = (cy - 6.5) + "px"; nm.style.color = col; nm.textContent = p.call;
      nm.title = "Click to solo this operator";
      // stop the press from reaching el.stack's scrubber (which would pause +
      // yank the playhead); the click still toggles solo
      nm.addEventListener("pointerdown", (e) => e.stopPropagation());
      nm.addEventListener("click", (e) => { e.stopPropagation(); setSolo(p.key === soloKey ? null : p.key); });
      el.rost.appendChild(nm);
    });
    // vote legend
    el.legend.innerHTML = "";
    if (hasVoter) {
      for (const [name, col] of clientColor) { const s = document.createElement("span"); s.className = "tm-lchip"; s.innerHTML = `<i style="background:${col}"></i>${escapeHtml(name)}`; el.legend.appendChild(s); }
    }
    el.voteLane.style.display = hasVoter ? "" : "none";
    el.legend.style.display = hasVoter ? "" : "none";
    el.geoWrap.hidden = !ctx.hasGeo;
  }

  // reposition DOM overlays for the current viewport / solo / search
  function layout() {
    for (const { o, el: d } of ovEls) {
      const f = vfrac(o.start);
      const off = f < -0.02 || f > 1.02;
      d.style.display = off ? "none" : ""; if (off) continue;
      d.style.left = f * 100 + "%";
      d.style.opacity = String(dimOf(o));
      d.classList.toggle("match", matchIds.has(o.id));
    }
    for (const { t, el: s } of mkEls) { const f = vfrac(t); const off = f < -0.02 || f > 1.02; s.style.display = off ? "none" : ""; if (!off) s.style.left = f * 100 + "%"; }
    for (const { o, el: b } of rbEls) {
      const f0 = vfrac(o.start), f1 = vfrac(o.end);
      const off = f1 < -0.02 || f0 > 1.02;
      b.style.display = off ? "none" : ""; if (off) continue;
      b.style.left = f0 * 100 + "%"; b.style.width = Math.max(0.35, (f1 - f0) * 100) + "%";
      b.style.opacity = String(0.55 * dimOf(o));
    }
    updateVRect();
  }

  function updateVRect() {
    const vr = el.rib.querySelector<HTMLElement>("[data-vrect]"); if (!vr) return;
    const full = vspan() >= SPAN - 1e-3;
    vr.style.display = full ? "none" : "block";
    if (!full) { vr.style.left = (V0 - T0) / SPAN * 100 + "%"; vr.style.width = vspan() / SPAN * 100 + "%"; }
  }

  // ---- geo inset ----
  const geoCache = new Map<number, GeoData | null | 0>(); // 0 = loading
  function ensureGeo(o: Over): GeoData | null {
    if (!ctx.hasGeo || !o.voter) return null;
    const c = geoCache.get(o.id);
    if (c === undefined) { geoCache.set(o.id, 0); api.geo(o.id).then((g) => geoCache.set(o.id, g)).catch(() => geoCache.set(o.id, null)); return null; }
    return c === 0 || c === null ? null : c;
  }
  function drawGeo(o: Over | null, L: number) {
    const cv = el.geo, W = cv.width, H = cv.height, c = cv.getContext("2d")!; c.clearRect(0, 0, W, H);
    const g = o ? ensureGeo(o) : null;
    if (!g || !g.receivers.length) {
      c.fillStyle = cvar("--text-faint"); c.font = "10px var(--mono, monospace)"; c.textAlign = "center";
      c.fillText(o && o.voter ? "locating…" : "— no fix —", W / 2, H / 2); c.textAlign = "left"; return;
    }
    const pts: [number, number][] = g.receivers.map((r) => [r.lat, r.lon]);
    for (const p of g.track) if (p.est) pts.push(p.est as [number, number]);
    pts.push(g.best_est);
    let minLat = Infinity, maxLat = -Infinity, minLon = Infinity, maxLon = -Infinity;
    for (const [la, lo] of pts) { minLat = Math.min(minLat, la); maxLat = Math.max(maxLat, la); minLon = Math.min(minLon, lo); maxLon = Math.max(maxLon, lo); }
    const pLat = (maxLat - minLat || 0.02) * 0.28, pLon = (maxLon - minLon || 0.02) * 0.28;
    minLat -= pLat; maxLat += pLat; minLon -= pLon; maxLon += pLon;
    const proj = (la: number, lo: number): [number, number] => [(lo - minLon) / (maxLon - minLon || 1) * W, (1 - (la - minLat) / (maxLat - minLat || 1)) * H];
    // current estimate — nearest track sample to L
    let est: [number, number] = g.best_est, bd = 1e9;
    for (const p of g.track) { if (!p.est) continue; const d = Math.abs(p.t - L); if (d < bd) { bd = d; est = p.est as [number, number]; } }
    const [ex, ey] = proj(est[0], est[1]);
    // fuzzy cloud — layered halos (entertainment, not DF)
    for (const [r, al] of [[34, 0.10], [22, 0.14], [12, 0.22]] as [number, number][]) {
      const grd = c.createRadialGradient(ex, ey, 0, ex, ey, r);
      grd.addColorStop(0, `rgba(255,120,120,${al})`); grd.addColorStop(1, "rgba(255,120,120,0)");
      c.fillStyle = grd; c.beginPath(); c.arc(ex, ey, r, 0, Math.PI * 2); c.fill();
    }
    c.fillStyle = "#ff6b6b"; c.beginPath(); c.arc(ex, ey, 2.5, 0, Math.PI * 2); c.fill();
    // receivers
    for (const r of g.receivers) {
      const [x, y] = proj(r.lat, r.lon);
      c.fillStyle = clientColor.get(r.name) || accent; c.beginPath(); c.arc(x, y, 2.5, 0, Math.PI * 2); c.fill();
      c.fillStyle = cvar("--text-dim"); c.font = "8px var(--mono, monospace)";
      c.fillText(r.name.slice(0, 10), Math.min(W - 42, x + 4), clamp(y + 3, 8, H - 2));
    }
  }

  // ---- render(T): every lane a pure function of T ----
  const wallFmt = (t: number) => new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  function render(t: number) {
    const f = clamp(vfrac(t), 0, 1);
    el.head.style.left = `calc(${LAB}px + (100% - ${LAB}px) * ${f})`;
    el.shade.style.left = LAB + "px"; el.shade.style.width = `calc((100% - ${LAB}px) * ${f})`;
    el.tc.firstChild!.nodeValue = wallFmt(t);
    const rel = Math.max(0, t - T0); el.tcSub.textContent = " +" + Math.floor(rel / 60) + ":" + String(Math.floor(rel % 60)).padStart(2, "0");
    const o = overAt(t), L = o ? t - o.start : 0;
    for (const { o: oo, el: d } of ovEls) d.style.textShadow = (o && oo === o) ? "0 0 8px currentColor" : "none";
    if (o) {
      const win = hasVoter ? winnerAt(o, t) : null;
      el.who.innerHTML = `<span class="tm-call" style="color:${spCol(o)}">${escapeHtml(o.call)}</span>` +
        (o.verified ? `<span class="tm-chip ${o.verified === "voice" ? "" : "solid"}">${o.verified === "voice" ? "? voice" : escapeHtml(o.verified)}</span>` : "") +
        (o.q ? `<span class="tm-chip">${escapeHtml(o.q)}${o.snr != null ? " · " + Math.round(o.snr) + " dB" : ""}</span>` : "") +
        (win ? `<span class="tm-chip vote"><i style="background:${clientColor.get(win)}"></i>${escapeHtml(win)}</span>` : "");
      if (o.words.length) {
        el.cap.className = "tm-cap";
        el.cap.innerHTML = o.words.map((w) => { const st = (L >= w[1] && L <= w[2]) ? "now" : (L > w[2] ? "done" : ""); return `<span class="w ${st}">${escapeHtml(w[0])}</span>`; }).join(" ");
      } else { el.cap.className = "tm-cap silent"; el.cap.textContent = o.q ? "(no speech — " + o.q + ")" : "(no transcript)"; }
    } else {
      el.who.innerHTML = `<span class="tm-idle">— dead air —</span>`;
      el.cap.className = "tm-cap silent"; el.cap.textContent = "(quiet · scrub to the next over)";
    }
    for (const { o: oo, el: b } of rbEls) { const onNow = !!(o && oo === o); b.style.boxShadow = onNow ? "0 0 9px currentColor" : "none"; }
    if (ctx.hasGeo) drawGeo(o, L);
  }

  function redraw() { drawWave(); drawSignal(); drawVote(); layout(); render(T); }
  function setViewport(nv0: number, nvs: number) { nvs = clamp(nvs, MINSPAN, SPAN); nv0 = clamp(nv0, T0, T1 - nvs); V0 = nv0; V1 = nv0 + nvs; redraw(); if (panel && !panel.hidden) refreshExpRange(); }

  // ---- audio: a SLAVE to the clock (unchanged from phase 2) ----
  const audioCache = new Map<number, HTMLAudioElement>();
  let curAudio: HTMLAudioElement | null = null, curId = -1, muted = false;
  function audioFor(o: Over): HTMLAudioElement | null {
    if (!o.hasAudio) return null;
    let a = audioCache.get(o.id);
    if (!a) {
      a = new Audio(api.audioUrl(o.id)); a.preload = "auto"; audioCache.set(o.id, a);
      while (audioCache.size > 5) { let done = true; for (const k of audioCache.keys()) { if (k !== o.id && k !== curId) { const e = audioCache.get(k)!; e.pause(); e.src = ""; audioCache.delete(k); done = false; break; } } if (done) break; }
    }
    return a;
  }
  const stopAudio = () => { if (curAudio && !curAudio.paused) curAudio.pause(); };
  function syncAudio() {
    const o = overAt(T);
    if (playing && o) { const nx = overs.find((x) => x.start > T && x.start < T + 2.5); if (nx) audioFor(nx); }
    if (!(playing && spd === 1 && o && o.hasAudio && passesSolo(o))) { stopAudio(); if (!o || o.id !== curId) { curAudio = null; curId = o ? o.id : -1; } return; }
    if (curId !== o.id || !curAudio) {
      stopAudio(); curAudio = audioFor(o); curId = o.id;
      if (curAudio) { curAudio.muted = muted; try { curAudio.currentTime = clamp(T - o.start, 0, o.dur); } catch { /* not seekable yet */ } curAudio.playbackRate = 1; curAudio.play().catch(() => {}); }
      return;
    }
    const target = T - o.start, drift = curAudio.currentTime - target;
    if (Math.abs(drift) > 0.25) { try { curAudio.currentTime = clamp(target, 0, o.dur); } catch { /* */ } curAudio.playbackRate = 1; }
    else curAudio.playbackRate = drift > 0.04 ? 0.96 : drift < -0.04 ? 1.04 : 1;
    curAudio.muted = muted;
    if (curAudio.paused) curAudio.play().catch(() => {});
  }

  // ---- transport / clock ----
  const validAt = (t: number) => { const o = overAt(t); return o && passesSolo(o) ? o : null; };
  const nextValid = (t: number): number | null => { let best: number | null = null; for (const o of overs) if (o.start > t + 1e-4 && passesSolo(o) && (best == null || o.start < best)) best = o.start; return best; };

  function seek(t: number) { T = clamp(t, T0, T1); if (T < V0 || T > V1) setViewport(T - vspan() * 0.3, vspan()); else render(T); }
  const fracFromEvent = (e: PointerEvent, node: HTMLElement, lab: number) => { const r = node.getBoundingClientRect(); const x = e.clientX - r.left - lab, w = r.width - lab; return Math.min(1, Math.max(0, x / w)); };

  let scrubbing = false;
  on(el.stack, "pointerdown", (e) => { scrubbing = true; el.stack.setPointerCapture(e.pointerId); pause(); seek(V0 + fracFromEvent(e, el.stack, LAB) * vspan()); });
  on(el.stack, "pointermove", (e) => { if (scrubbing) seek(V0 + fracFromEvent(e, el.stack, LAB) * vspan()); });
  on(el.stack, "pointerup", () => { scrubbing = false; });
  on(el.stack, "wheel", (e) => {
    e.preventDefault();
    const r = el.stack.getBoundingClientRect(); const fx = clamp((e.clientX - r.left - LAB) / (r.width - LAB), 0, 1);
    const anchor = V0 + fx * vspan(); const k = e.deltaY > 0 ? 1.2 : 1 / 1.2;
    const ns = clamp(vspan() * k, MINSPAN, SPAN); setViewport(anchor - fx * ns, ns);
  }, { passive: false });

  // ribbon: click scrubs; the viewport rect drags to pan
  on(el.rib, "pointerdown", (e) => { if ((e.target as HTMLElement).dataset.vrect !== undefined) return; el.rib.setPointerCapture(e.pointerId); pause(); seek(T0 + fracFromEvent(e, el.rib, 0) * SPAN); });
  on(el.rib, "pointermove", (e) => { if (e.buttons && (e.target as HTMLElement).dataset.vrect === undefined) { pause(); seek(T0 + fracFromEvent(e, el.rib, 0) * SPAN); } });
  const vr = el.rib.querySelector<HTMLElement>("[data-vrect]");
  if (vr) {
    let panX = 0, panV0 = 0, panning = false;
    on(vr, "pointerdown", (e) => { e.stopPropagation(); panning = true; vr.setPointerCapture(e.pointerId); panX = e.clientX; panV0 = V0; });
    on(vr, "pointermove", (e) => { if (!panning) return; const w = el.rib.getBoundingClientRect().width || 1; setViewport(panV0 + (e.clientX - panX) / w * SPAN, vspan()); });
    on(vr, "pointerup", () => { panning = false; });
  }

  const loop = (ts: number) => {
    if (!playing) return;
    const dt = (ts - last) / 1000; last = ts; T += dt * spd;
    if (ripple || soloKey) { if (!validAt(T)) { const nx = nextValid(T); if (nx == null) { T = T1; } else T = nx; } }
    if (T >= T1) { T = T1; render(T); syncAudio(); pause(); return; }
    if (T < V0 || T > V1) setViewport(T - vspan() * 0.15, vspan()); else render(T);
    syncAudio();
    raf = requestAnimationFrame(loop);
  };
  function play() { playing = true; el.play.textContent = "❚❚ Pause"; last = performance.now(); syncAudio(); raf = requestAnimationFrame(loop); }
  function pause() { playing = false; el.play.textContent = "▶ Play"; stopAudio(); cancelAnimationFrame(raf); }
  on(el.play, "click", () => (playing ? pause() : play()));

  const deck = el.stack.closest(".tm-deck") as HTMLElement;
  const q = <T2 extends HTMLElement>(sel: string) => deck.querySelector<T2>(sel);
  const spdBtns = deck.querySelectorAll<HTMLButtonElement>(".spd");
  spdBtns.forEach((b) => on(b, "click", () => { spd = +(b.dataset.spd || "1"); spdBtns.forEach((x) => x.classList.remove("on")); b.classList.add("on"); }));
  const ripBtn = q<HTMLButtonElement>("[data-ripple]");
  if (ripBtn) on(ripBtn, "click", () => { ripple = !ripple; ripBtn.classList.toggle("on", ripple); });
  const muteBtn = q<HTMLButtonElement>("[data-mute]");
  if (muteBtn) on(muteBtn, "click", () => { muted = !muted; if (curAudio) curAudio.muted = muted; muteBtn.textContent = muted ? "🔇 Muted" : "🔊 Audio"; muteBtn.classList.toggle("on", muted); });

  // zoom buttons
  deck.querySelectorAll<HTMLButtonElement>("[data-zoom]").forEach((b) => on(b, "click", () => {
    const z = b.dataset.zoom;
    if (z === "fit") setViewport(T0, SPAN);
    else { const k = z === "in" ? 1 / 1.6 : 1.6; const mid = clamp(T, V0, V1); const ns = clamp(vspan() * k, MINSPAN, SPAN); setViewport(mid - (mid - V0) / vspan() * ns, ns); }
  }));

  // search
  const searchEl = q<HTMLInputElement>("[data-search]"); const searchN = q<HTMLElement>("[data-searchn]");
  function runSearch() {
    const s = (searchEl?.value || "").trim().toLowerCase();
    matchList = s ? overs.filter((o) => o.text.includes(s)) : [];
    matchIds = new Set(matchList.map((o) => o.id));
    if (searchN) searchN.textContent = s ? `${matchList.length}` : "";
    drawRibbon(); layout();
  }
  function jumpNext() { if (!matchList.length) return; const nx = matchList.find((o) => o.start > T + 0.01) || matchList[0]; seek(nx.start + 0.05); }
  if (searchEl) { on(searchEl, "input", runSearch); on(searchEl, "keydown", (e) => { if ((e as KeyboardEvent).key === "Enter") { e.preventDefault(); jumpNext(); } }); }

  // solo
  const soloChip = q<HTMLButtonElement>("[data-solochip]");
  function setSolo(k: string | null) {
    soloKey = k;
    if (soloChip) { const on2 = !!k; soloChip.hidden = !on2; if (on2) { const label = overs.find((o) => o.key === k)?.call || "solo"; soloChip.textContent = `◉ solo: ${label} ✕`; } }
    drawRibbon(); redraw();
  }
  if (soloChip) on(soloChip, "click", () => setSolo(null));

  // ---- export (phase 5) ----
  let pollTimer = 0, disposed = false;
  const exportBtn = q<HTMLButtonElement>("[data-export]");
  const panel = q<HTMLElement>("[data-exportpanel]");
  const expRange = q<HTMLElement>("[data-exprange]"), expCap = q<HTMLInputElement>("[data-expcap]");
  const expRender = q<HTMLButtonElement>("[data-exprender]"), expProg = q<HTMLElement>("[data-expprog]");
  const expBar = q<HTMLElement>("[data-expbar]"), expMsg = q<HTMLElement>("[data-expmsg]"), expDl = q<HTMLAnchorElement>("[data-expdl]");
  function refreshExpRange() {
    if (!expRange) return;
    const mins = vspan() / 60;
    expRange.textContent = `${wallFmt(V0)} – ${wallFmt(V1)} · ${mins < 1 ? Math.round(vspan()) + "s" : mins.toFixed(1) + "m"}`;
    const tooLong = vspan() > EXPORT_MAX;
    if (expRender) expRender.disabled = tooLong;
    if (expMsg && tooLong) expMsg.textContent = `zoom in to ≤ ${EXPORT_MAX / 60} min to render`;
    else if (expMsg && expMsg.dataset.sticky !== "1") expMsg.textContent = "";
  }
  if (exportBtn && panel) on(exportBtn, "click", () => { panel.hidden = !panel.hidden; if (!panel.hidden) refreshExpRange(); });
  if (expRender) on(expRender, "click", async () => {
    if (expMsg) { expMsg.dataset.sticky = "0"; expMsg.textContent = "queued…"; }
    if (expDl) expDl.hidden = true;
    if (expProg) expProg.hidden = false; if (expBar) expBar.style.width = "0%";
    expRender.disabled = true;
    try {
      const { job_id } = await api.tmExport(V0, V1, !!expCap?.checked);
      const poll = async () => {
        try {
          const st = await api.tmExportStatus(job_id);
          if (disposed) return; // the modal closed while this request was in flight
          if (st.status === "rendering" || st.status === "queued") {
            if (expMsg) expMsg.textContent = st.status === "queued" ? "waiting for the transcriber to idle…" : "rendering…";
            if (expBar) expBar.style.width = Math.round((st.progress || 0) * 100) + "%";
            pollTimer = window.setTimeout(poll, 1000);
          } else if (st.status === "done") {
            if (expBar) expBar.style.width = "100%";
            if (expMsg) { expMsg.dataset.sticky = "1"; expMsg.textContent = "ready"; }
            if (expDl) { expDl.hidden = false; expDl.href = api.tmExportFileUrl(job_id); }
            expRender.disabled = false; refreshExpRange();
          } else { throw new Error(st.error || "render failed"); }
        } catch (err) {
          if (expMsg) { expMsg.dataset.sticky = "1"; expMsg.textContent = (err as Error).message || "render failed"; }
          if (expProg) expProg.hidden = true; expRender.disabled = false;
        }
      };
      poll();
    } catch (err) {
      if (expMsg) { expMsg.dataset.sticky = "1"; expMsg.textContent = (err as Error).message || "could not start"; }
      if (expProg) expProg.hidden = true; expRender.disabled = false;
    }
  });

  const rl = el.rib.querySelector<HTMLElement>("[data-riblabel]");
  if (rl) rl.textContent = `${overs.length} overs · ${wallFmt(T0)}–${wallFmt(T1)} · scroll to zoom · drag to scrub`;

  buildOverlays(); drawRibbon(); redraw();

  return () => {
    disposed = true;
    pause();
    if (pollTimer) clearTimeout(pollTimer);
    for (const f of cleanups) f();
    for (const a of audioCache.values()) { try { a.pause(); a.src = ""; } catch { /* */ } } audioCache.clear();
  };
}

function escapeHtml(s: string) { return s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c] as string)); }
