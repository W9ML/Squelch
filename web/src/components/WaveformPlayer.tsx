"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import { fmtClock, isPhosphor } from "@/lib/format";
import { playBus } from "@/lib/playqueue";
import type { Transmission } from "@/lib/types";
import { useApp } from "@/state/app-context";

type Aud = HTMLAudioElement & { _txId?: number };

/** the single recording that's currently sounding, so starting one pauses the
 *  other (module-level, mirroring the original's `nowPlaying`). */
let activeAudio: Aud | null = null;

/** Toggle the currently-sounding recording (used by the Space shortcut).
 *  Returns false when nothing has been played yet, so the caller can start
 *  the newest over instead. */
export function toggleActiveAudio(): boolean {
  if (!activeAudio) return false;
  if (activeAudio.paused) activeAudio.play().catch(() => {});
  else activeAudio.pause();
  return true;
}

// playback speed — the chosen rate carries to the next recording you play
const RATE_KEY = "squelch-rate";
const RATES = [1, 1.5, 2];
function loadRate(): number {
  try {
    const v = Number(localStorage.getItem(RATE_KEY));
    return RATES.includes(v) ? v : 1;
  } catch {
    return 1;
  }
}
let preferredRate = typeof window !== "undefined" ? loadRate() : 1;

function waveColors(): [string, string] {
  const cs = getComputedStyle(document.documentElement);
  return [
    cs.getPropertyValue("--wave-played").trim() || "#38bdf8",
    cs.getPropertyValue("--wave-bg").trim() || "#31405a",
  ];
}

function drawWave(
  canvas: HTMLCanvasElement,
  peaks: number[] | null,
  playedFrac: number,
  crt = false,
) {
  const cssW = canvas.clientWidth;
  const cssH = canvas.clientHeight;
  if (!cssW) return;
  const dpr = window.devicePixelRatio || 1;
  const resized = canvas.width !== cssW * dpr || canvas.height !== cssH * dpr;
  if (resized) {
    canvas.width = cssW * dpr;
    canvas.height = cssH * dpr;
  }
  // entering scope mode must start from a clean tube — otherwise the
  // previous theme's bar waveform stays ghosted under the persistence wash
  const mode = crt ? "crt" : "bars";
  const modeChanged = canvas.dataset.mode !== mode;
  canvas.dataset.mode = mode;
  const ctx = canvas.getContext("2d");
  if (!ctx) return;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  if (crt) {
    drawScope(ctx, cssW, cssH, peaks, playedFrac, resized || modeChanged);
    return;
  }
  ctx.clearRect(0, 0, cssW, cssH);
  const [played, bg] = waveColors();
  const barW = 2.5;
  const gap = 1.5;
  const stepW = barW + gap;
  const n = Math.max(1, Math.floor(cssW / stepW));
  const mid = cssH / 2;
  for (let i = 0; i < n; i++) {
    let v = 0.06;
    if (peaks && peaks.length) {
      const a = Math.floor((i * peaks.length) / n);
      const b = Math.max(a + 1, Math.floor(((i + 1) * peaks.length) / n));
      for (let j = a; j < b; j++) v = Math.max(v, peaks[j]);
    }
    const h = Math.max(2, v * (cssH - 4));
    ctx.fillStyle = (i + 0.5) / n <= playedFrac ? played : bg;
    ctx.beginPath();
    ctx.roundRect(i * stepW, mid - h / 2, barW, h, 1.2);
    ctx.fill();
  }
}

/** Phosphor CRT theme: the waveform as a green-P1 scope trace.
 *  Persistence comes from fading the previous frame with a translucent
 *  wash instead of clearing — during playback the sweeping beam leaves
 *  ghost trails that decay, exactly like slow phosphor. A faint
 *  chromatic double-strike under the played trace fakes the tube's
 *  convergence error (the "bloom"). */
function drawScope(
  ctx: CanvasRenderingContext2D,
  cssW: number,
  cssH: number,
  peaks: number[] | null,
  playedFrac: number,
  resized: boolean,
) {
  const [played, bg] = waveColors();
  if (resized) ctx.clearRect(0, 0, cssW, cssH);
  else {
    // phosphor decay: dim what's already on the tube instead of wiping it
    ctx.fillStyle = "rgba(5, 10, 6, 0.32)";
    ctx.fillRect(0, 0, cssW, cssH);
  }
  const mid = cssH / 2;
  // graticule center line
  ctx.strokeStyle = "rgba(61, 255, 124, 0.12)";
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(0, mid);
  ctx.lineTo(cssW, mid);
  ctx.stroke();

  const n = Math.max(2, Math.floor(cssW / 2));
  const amp = (i: number) => {
    let v = 0.04;
    if (peaks && peaks.length) {
      const a = Math.floor((i * peaks.length) / n);
      const b = Math.max(a + 1, Math.floor(((i + 1) * peaks.length) / n));
      for (let j = a; j < b; j++) v = Math.max(v, peaks[j]);
    }
    return Math.max(1, v * (cssH / 2 - 2));
  };
  const trace = (fromFrac: number, toFrac: number, dx: number) => {
    const i0 = Math.floor(fromFrac * (n - 1));
    const i1 = Math.ceil(toFrac * (n - 1));
    ctx.beginPath();
    for (let i = i0; i <= i1; i++) {
      const x = (i / (n - 1)) * cssW + dx;
      const y = mid - amp(i);
      if (i === i0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    }
    for (let i = i1; i >= i0; i--) {
      ctx.lineTo((i / (n - 1)) * cssW + dx, mid + amp(i));
    }
    ctx.closePath();
    ctx.stroke();
  };

  // unplayed: dim trace, no glow
  ctx.shadowBlur = 0;
  ctx.lineWidth = 1;
  ctx.strokeStyle = bg;
  trace(playedFrac, 1, 0);
  if (playedFrac > 0) {
    // chromatic double-strike under the bright trace (convergence error)
    ctx.lineWidth = 1;
    ctx.strokeStyle = "rgba(255, 110, 90, 0.16)";
    trace(0, playedFrac, -0.8);
    ctx.strokeStyle = "rgba(90, 170, 255, 0.14)";
    trace(0, playedFrac, 0.8);
    // the beam-lit portion, glowing
    ctx.shadowColor = played;
    ctx.shadowBlur = 7;
    ctx.lineWidth = 1.4;
    ctx.strokeStyle = played;
    trace(0, playedFrac, 0);
    // the beam itself
    const bx = playedFrac * cssW;
    ctx.shadowBlur = 12;
    ctx.lineWidth = 1.6;
    ctx.beginPath();
    ctx.moveTo(bx, 2);
    ctx.lineTo(bx, cssH - 2);
    ctx.stroke();
    ctx.shadowBlur = 0;
  }
}

export function WaveformPlayer({ tx, bus }: { tx: Transmission; bus: EventTarget }) {
  const { theme } = useApp();
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const timeRef = useRef<HTMLSpanElement>(null);
  const audioRef = useRef<Aud | null>(null);
  // Say Again tap-to-loop: while set, playback loops within [s,e]
  const loopRef = useRef<{ s: number; e: number } | null>(null);
  const [isPlaying, setPlaying] = useState(false);
  const [loading, setLoading] = useState(false);
  const [rate, setRate] = useState(preferredRate);
  const totalS = (tx.duration_ms || 0) / 1000;
  const fracRef = useRef(0);

  // apply speed changes to the live audio element
  useEffect(() => {
    if (audioRef.current) audioRef.current.playbackRate = rate;
  }, [rate]);

  const cycleRate = () => {
    const next = RATES[(RATES.indexOf(rate) + 1) % RATES.length];
    setRate(next);
    preferredRate = next;
    try {
      localStorage.setItem(RATE_KEY, String(next));
    } catch {
      /* ignore */
    }
  };

  const crt = isPhosphor(theme); // green/amber/cyan get the scope-trace waveform
  const redraw = useCallback(
    (frac: number) => {
      if (canvasRef.current) drawWave(canvasRef.current, tx.peaks, frac, crt);
    },
    [tx.peaks, crt],
  );

  const setTimeLabel = (s: string) => {
    if (timeRef.current) timeRef.current.textContent = s;
  };

  // initial paint + repaint on theme change + on resize
  useEffect(() => {
    redraw(fracRef.current);
    const onResize = () => redraw(fracRef.current);
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, [redraw, theme]);

  const wire = useCallback(
    (a: Aud) => {
      a.ontimeupdate = () => {
        const lp = loopRef.current;
        if (lp && a.currentTime >= lp.e) a.currentTime = lp.s; // tap-to-loop
        const dur = isFinite(a.duration) && a.duration ? a.duration : totalS;
        const frac = dur ? a.currentTime / dur : 0;
        fracRef.current = frac;
        redraw(frac);
        setTimeLabel(`${fmtClock(a.currentTime)} / ${fmtClock(dur)}`);
        bus.dispatchEvent(new CustomEvent("ptime", { detail: a.currentTime }));
      };
      a.onplay = () => {
        if (activeAudio && activeAudio !== a) activeAudio.pause();
        activeAudio = a;
        setPlaying(true);
        bus.dispatchEvent(new CustomEvent("pplay"));
      };
      a.onpause = () => {
        setPlaying(false);
        bus.dispatchEvent(new CustomEvent("ppause"));
      };
      a.onended = () => {
        fracRef.current = 0;
        redraw(0);
        setTimeLabel(fmtClock(totalS));
        setPlaying(false);
        bus.dispatchEvent(new CustomEvent("pended"));
        if (activeAudio === a) activeAudio = null;
      };
      a.onwaiting = () => setLoading(true);
      a.onplaying = () => setLoading(false);
    },
    [bus, redraw, totalS],
  );

  const ensureAudio = useCallback((): Aud => {
    if (audioRef.current) return audioRef.current;
    const a = new Audio(api.audioUrl(tx.id)) as Aud;
    a._txId = tx.id;
    a.preload = "auto";
    a.playbackRate = preferredRate;
    wire(a);
    audioRef.current = a;
    return a;
  }, [tx.id, wire]);

  // keep the audio element's handlers fresh: they capture redraw (and its
  // CRT-scope flag) at wire time, so without this a theme switch after the
  // element exists keeps painting the OLD waveform mode during playback
  useEffect(() => {
    if (audioRef.current) wire(audioRef.current);
  }, [wire]);

  // pause on unmount if this card owns the active audio (e.g. filtered out)
  useEffect(() => {
    return () => {
      const a = audioRef.current;
      if (a) {
        a.pause();
        if (activeAudio === a) activeAudio = null;
      }
    };
  }, []);

  // another card (auto-advance or the Space shortcut) asked us to start
  useEffect(() => {
    const onPlay = (e: Event) => {
      if ((e as CustomEvent<number>).detail !== tx.id) return;
      const a = ensureAudio();
      if (a.paused) a.play().catch(() => {});
    };
    playBus.addEventListener("play", onPlay);
    return () => playBus.removeEventListener("play", onPlay);
  }, [tx.id, ensureAudio]);

  // karaoke word click -> seek here
  useEffect(() => {
    const onSeek = (e: Event) => {
      const detail = (e as CustomEvent<number>).detail;
      const a = ensureAudio();
      loopRef.current = null; // a plain seek cancels any active loop
      const jump = () => {
        a.currentTime = detail;
        if (a.paused) a.play();
      };
      if (isFinite(a.duration) && a.duration) jump();
      else {
        a.addEventListener("loadedmetadata", jump, { once: true });
        a.load();
      }
    };
    bus.addEventListener("seekto", onSeek);
    return () => bus.removeEventListener("seekto", onSeek);
  }, [bus, ensureAudio]);

  // Say Again: replay just a callsign's span, looped, until the user does
  // anything else (play/pause, scrub, or a plain word-seek)
  useEffect(() => {
    const onLoop = (e: Event) => {
      const { s, e: end } = (e as CustomEvent<{ s: number; e: number }>).detail;
      const a = ensureAudio();
      const start = () => {
        const dur = isFinite(a.duration) && a.duration ? a.duration : totalS;
        const lo = Math.max(0, s - 0.15);
        const hi = Math.min((dur || end + 0.4) - 0.05, end + 0.35);
        loopRef.current = { s: lo, e: hi > lo ? hi : lo + 0.4 };
        a.currentTime = lo;
        a.play().catch(() => {});
      };
      if (isFinite(a.duration) && a.duration) start();
      else {
        a.addEventListener("loadedmetadata", start, { once: true });
        a.load();
      }
    };
    bus.addEventListener("loopspan", onLoop);
    return () => bus.removeEventListener("loopspan", onLoop);
  }, [bus, ensureAudio, totalS]);

  // ---- scrubbing ----
  const scrub = useRef({ active: false, moved: false, wasPlaying: false, startX: 0 });
  const fracAt = (e: React.PointerEvent) => {
    const c = canvasRef.current!;
    const r = c.getBoundingClientRect();
    return Math.max(0, Math.min(1, (e.clientX - r.left) / r.width));
  };
  const preview = (frac: number) => {
    const a = ensureAudio();
    fracRef.current = frac;
    redraw(frac);
    const dur = isFinite(a.duration) && a.duration ? a.duration : totalS;
    setTimeLabel(`${fmtClock(frac * dur)} / ${fmtClock(dur)}`);
    if (isFinite(a.duration) && a.duration) a.currentTime = frac * a.duration;
  };

  if (!tx.has_audio) {
    return (
      <div className="player expired">
        <canvas ref={canvasRef} className="wave" />
        <span className="expired-note">audio expired</span>
      </div>
    );
  }

  return (
    <div className="player">
      <button
        className={"play-btn" + (loading ? " loading" : "")}
        title={isPlaying ? "Pause" : "Play"}
        onClick={() => {
          const a = ensureAudio();
          loopRef.current = null;
          if (a.paused) a.play();
          else a.pause();
        }}
      >
        <svg viewBox="0 0 24 24" aria-hidden="true">
          {isPlaying ? (
            <>
              <rect x="6" y="5" width="4" height="14" rx="1" />
              <rect x="14" y="5" width="4" height="14" rx="1" />
            </>
          ) : (
            <path d="M8 5.5v13a.6.6 0 0 0 .9.5l10.4-6.5a.6.6 0 0 0 0-1L8.9 5a.6.6 0 0 0-.9.5z" />
          )}
        </svg>
      </button>
      <canvas
        ref={canvasRef}
        className="wave"
        onPointerDown={(e) => {
          e.preventDefault();
          const a = ensureAudio();
          loopRef.current = null; // scrubbing cancels any active loop
          scrub.current = {
            active: true,
            moved: false,
            wasPlaying: !a.paused,
            startX: e.clientX,
          };
          a.pause();
          try {
            (e.target as HTMLElement).setPointerCapture(e.pointerId);
          } catch {}
          preview(fracAt(e));
        }}
        onPointerMove={(e) => {
          if (!scrub.current.active) return;
          if (Math.abs(e.clientX - scrub.current.startX) > 3) scrub.current.moved = true;
          preview(fracAt(e));
        }}
        onPointerUp={(e) => {
          if (!scrub.current.active) return;
          scrub.current.active = false;
          try {
            (e.target as HTMLElement).releasePointerCapture(e.pointerId);
          } catch {}
          const frac = fracAt(e);
          const play = scrub.current.moved ? scrub.current.wasPlaying : true;
          const a = ensureAudio();
          redraw(frac);
          const commit = () => {
            a.currentTime = frac * a.duration;
            if (play) a.play();
          };
          if (isFinite(a.duration) && a.duration) commit();
          else {
            a.addEventListener("loadedmetadata", commit, { once: true });
            a.load();
          }
        }}
      />
      <span ref={timeRef} className="ptime">
        {fmtClock(totalS)}
      </span>
      <button
        className="speed-btn"
        title="Playback speed"
        aria-label={`Playback speed ${rate}×`}
        onClick={cycleRate}
      >
        {rate}×
      </button>
    </div>
  );
}
