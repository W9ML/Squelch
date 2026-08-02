"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { isCoarsePointer } from "./pointer";

// start scheduling audio this many seconds ahead of the current playback clock
const BUFFER_AHEAD_S = 0.1;

function wsBase(): string {
  return (
    process.env.NEXT_PUBLIC_WS_BASE ||
    `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}`
  );
}

// live stream loudness — deliberately separate from the alert-tone volume so
// you can mute the dings while listening. Module-level like sound.ts, so the
// alerts popover can adjust it without reaching into the hook.
const LIVE_VOL_KEY = "squelch-live-volume";

function loadLiveVolume(): number {
  try {
    const v = Number(localStorage.getItem(LIVE_VOL_KEY));
    return isFinite(v) && v >= 0 && v <= 1 ? v : 1;
  } catch {
    return 1;
  }
}

let liveVolume = typeof window !== "undefined" ? loadLiveVolume() : 1;
let liveGain: GainNode | null = null;

// a volume change in another tab should also apply to a stream playing here
// (storage events fire in every tab except the one that wrote the value)
if (typeof window !== "undefined") {
  window.addEventListener("storage", (e) => {
    if (e.key === LIVE_VOL_KEY && e.newValue !== null) {
      const v = Number(e.newValue);
      if (isFinite(v) && v >= 0 && v <= 1) {
        liveVolume = v;
        if (liveGain) liveGain.gain.value = v;
      }
    }
  });
}

export function getLiveVolume(): number {
  return liveVolume;
}

export function setLiveVolume(v: number): void {
  liveVolume = Math.max(0, Math.min(1, v));
  try {
    localStorage.setItem(LIVE_VOL_KEY, String(liveVolume));
  } catch {
    /* ignore */
  }
  if (liveGain) liveGain.gain.value = liveVolume;
}

/** Carry-over state for the fallback resampler: the previous chunk's final
 *  sample and the fractional read position into the next chunk, so linear
 *  interpolation stays continuous across chunk boundaries. Without this,
 *  each 20 ms chunk gets resampled in isolation and the seams buzz at 50 Hz. */
interface ResampleState {
  last: number;
  pos: number;
}

/** Linearly resample one chunk, carrying interpolation state in `st`.
 *  `ratio` is source samples per output sample (inRate / outRate). */
function resampleChunk(input: Float32Array, ratio: number, st: ResampleState): Float32Array {
  const n = input.length;
  const out = new Float32Array(Math.ceil((n - st.pos) / ratio) + 2);
  let o = 0;
  let pos = st.pos; // fractional index into input; index -1 is st.last
  while (pos < n - 1) {
    const i = Math.floor(pos);
    const frac = pos - i;
    const s0 = i < 0 ? st.last : input[i];
    const s1 = input[i + 1];
    out[o++] = s0 + (s1 - s0) * frac;
    pos += ratio;
  }
  st.last = input[n - 1];
  st.pos = pos - n; // relative to the start of the next chunk
  return out.subarray(0, o);
}

export interface LiveAudioState {
  /** WebSocket is connected and we're actively listening */
  listening: boolean;
  /** A transmission is currently streaming through */
  streaming: boolean;
  toggleListen: () => void;
}

export function useLiveAudio(): LiveAudioState {
  const [listening, setListening] = useState(false);
  const [streaming, setStreaming] = useState(false);

  const wsRef = useRef<WebSocket | null>(null);
  const ctxRef = useRef<AudioContext | null>(null);
  const playheadRef = useRef<number>(0);
  const sampleRateRef = useRef<number>(8000);
  const resampleRef = useRef<ResampleState>({ last: 0, pos: 0 });
  const underrunsRef = useRef<number>(0);

  const stop = useCallback(() => {
    const ws = wsRef.current;
    const ctx = ctxRef.current;
    wsRef.current = null;
    ctxRef.current = null;
    liveGain = null;
    if (ws) {
      // detach before closing: the close event lands a network round-trip
      // later, and a stale onclose must not clobber a session started since
      ws.onopen = ws.onmessage = ws.onerror = ws.onclose = null;
      try { ws.close(); } catch { /* noop */ }
    }
    ctx?.close().catch(() => {});
    setListening(false);
    setStreaming(false);
  }, []);

  const start = useCallback(() => {
    // a previous session's context can survive a remote close — never stack
    ctxRef.current?.close().catch(() => {});
    // AudioContext must be created inside a user-gesture handler. Ask for the
    // stream's native 8 kHz so scheduled chunks are stitched sample-accurately
    // and the browser resamples the *continuous* output once — per-chunk
    // resampling (a default 48 kHz context playing 8 kHz buffers) restarts the
    // interpolator at every 20 ms seam, which is loudly audible.
    let ctx: AudioContext;
    try {
      ctx = new AudioContext({ sampleRate: 8000 });
    } catch {
      try {
        ctx = new AudioContext(); // browser refused 8 kHz — resampleChunk covers it
      } catch {
        return; // no audio contexts available (mobile cap) — stay stopped
      }
    }
    ctxRef.current = ctx;
    const gain = ctx.createGain();
    // phones: full app volume, the hardware rocker is the volume control —
    // an in-app attenuator on top of the OS media volume just double-dips
    gain.gain.value = isCoarsePointer() ? 1 : liveVolume;
    gain.connect(ctx.destination);
    liveGain = gain;
    playheadRef.current = 0;
    resampleRef.current = { last: 0, pos: 0 };

    const ws = new WebSocket(`${wsBase()}/ws/audio`);
    ws.binaryType = "arraybuffer";
    wsRef.current = ws;

    ws.onopen = () => setListening(true);
    ws.onclose = () => {
      // remote close (backend restart, dropped Wi-Fi): full cleanup, and only
      // if this socket still owns the session
      if (wsRef.current !== ws) return;
      wsRef.current = null;
      ctxRef.current = null;
      liveGain = null;
      ctx.close().catch(() => {});
      setListening(false);
      setStreaming(false);
    };
    ws.onerror = () => { try { ws.close(); } catch { /* noop */ } };

    ws.onmessage = (ev) => {
      if (typeof ev.data === "string") {
        try {
          const msg = JSON.parse(ev.data) as { type: string; sample_rate?: number };
          if (msg.type === "start") {
            sampleRateRef.current = msg.sample_rate ?? 8000;
            resampleRef.current = { last: 0, pos: 0 };
            underrunsRef.current = 0;
            // reset playhead at the start of each TX with a small jitter
            // buffer — never rewinding over a previous TX's still-playing tail
            playheadRef.current = Math.max(
              playheadRef.current, ctx.currentTime + BUFFER_AHEAD_S);
            setStreaming(true);
          } else if (msg.type === "end") {
            setStreaming(false);
          }
        } catch { /* ignore malformed control frames */ }
        return;
      }

      if (!(ev.data instanceof ArrayBuffer)) return;
      const samples = new Int16Array(ev.data);
      if (!samples.length) return;
      setStreaming(true); // covers joining mid-transmission (no "start" seen)

      // browsers suspend AudioContexts (backgrounded tab, phone screen off).
      // A suspended context's clock is frozen, so scheduling against it would
      // pile chunks onto the same instant and garble on resume — drop instead
      // (it's live audio; the listener wasn't hearing those chunks anyway)
      if (ctx.state !== "running") {
        ctx.resume().catch(() => {});
        return;
      }

      const sr = sampleRateRef.current;
      let pcm: Float32Array<ArrayBufferLike> = new Float32Array(samples.length);
      for (let i = 0; i < samples.length; i++) {
        pcm[i] = samples[i] / 32768;
      }
      if (sr !== ctx.sampleRate) {
        pcm = resampleChunk(pcm, sr / ctx.sampleRate, resampleRef.current);
        if (!pcm.length) return;
      }
      const buf = ctx.createBuffer(1, pcm.length, ctx.sampleRate);
      buf.getChannelData(0).set(pcm);

      // never fall behind real time — and if we did (network hiccup or a
      // mid-transmission join), fade the chunk in so the jump isn't a click.
      // Re-inflate the jitter buffer on each underrun (a little more each
      // time) — clamping to "now" would leave a 20 ms cushion and turn every
      // later wiggle of network jitter into another audible gap.
      const floor = ctx.currentTime + 0.02;
      let when = playheadRef.current;
      if (when < floor) {
        underrunsRef.current += 1;
        when = ctx.currentTime +
          Math.min(BUFFER_AHEAD_S * underrunsRef.current, 0.5);
        const ch = buf.getChannelData(0);
        const ramp = Math.min(64, ch.length);
        for (let i = 0; i < ramp; i++) ch[i] *= i / ramp;
      }
      const src = ctx.createBufferSource();
      src.buffer = buf;
      src.connect(gain);
      src.start(when);
      playheadRef.current = when + buf.length / ctx.sampleRate;
    };
  }, []);

  const toggleListen = useCallback(() => {
    if (wsRef.current) {
      stop();
    } else {
      start();
    }
  }, [start, stop]);

  useEffect(() => () => { stop(); }, [stop]);

  return { listening, streaming, toggleListen };
}
