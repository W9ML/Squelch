"use client";

/**
 * Subtle alert tones, synthesized with the Web Audio API — no asset files, so
 * nothing to load and no CSP media-src needed. A single shared AudioContext is
 * created lazily and must be resumed after a user gesture (browser autoplay
 * policy); see unlockAudio().
 *
 * Loudness is a user setting (0..1) from the header slider: the final peak of
 * each tone is its base gain scaled by that volume. Base gains go high (up to
 * ~0.9) and the two-note alert is SEQUENTIAL (no overlap) so peaks never sum
 * past full scale and distort.
 */

import { isCoarsePointer } from "./pointer";

let ctx: AudioContext | null = null;
let lastPlay = 0; // shared throttle so live alerts never machine-gun
let lastPreview = 0; // separate, tighter throttle for slider drag ticks

export const DEFAULT_VOLUME = 0.85;
let userVolume = DEFAULT_VOLUME;

export function setUserVolume(v: number): void {
  userVolume = Math.max(0, Math.min(1, v));
}

function getCtx(): AudioContext | null {
  if (typeof window === "undefined") return null;
  if (!ctx) {
    const AC =
      window.AudioContext ||
      (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
    if (!AC) return null;
    ctx = new AC();
  }
  return ctx;
}

/** Resume the context (call from a user gesture — click, keypress, the slider). */
export function unlockAudio(): void {
  const c = getCtx();
  if (c && c.state === "suspended") c.resume().catch(() => {});
}

interface ToneOpts {
  freq: number;
  dur: number;
  gain: number; // base 0..1; scaled by userVolume
  type?: OscillatorType;
  glideTo?: number;
  delay?: number;
}

function tone(c: AudioContext, o: ToneOpts): void {
  const t0 = c.currentTime + (o.delay || 0);
  const osc = c.createOscillator();
  const g = c.createGain();
  osc.type = o.type || "sine";
  osc.frequency.setValueAtTime(o.freq, t0);
  if (o.glideTo) osc.frequency.exponentialRampToValueAtTime(o.glideTo, t0 + o.dur);
  // phones have no volume slider — tones ride at base gain under the OS volume
  const vol = isCoarsePointer() ? 1 : userVolume;
  const peak = Math.max(0.0002, Math.min(0.95, o.gain * vol));
  g.gain.setValueAtTime(0.0001, t0);
  g.gain.exponentialRampToValueAtTime(peak, t0 + 0.012); // soft attack
  g.gain.exponentialRampToValueAtTime(0.0001, t0 + o.dur); // smooth decay
  osc.connect(g).connect(c.destination);
  osc.start(t0);
  osc.stop(t0 + o.dur + 0.02);
}

function ready(minGap = 300): AudioContext | null {
  const c = getCtx();
  if (!c || c.state !== "running") return null;
  const now = performance.now();
  if (now - lastPlay < minGap) return null;
  lastPlay = now;
  return c;
}

/** the "new recording" alert: two sequential rising notes (D5 -> A5). */
function newRecordingTones(c: AudioContext): void {
  tone(c, { freq: 587.33, dur: 0.12, gain: 0.9 });
  tone(c, { freq: 880.0, dur: 0.15, gain: 0.8, delay: 0.12 });
}

export function playNewRecording(): void {
  const c = ready(300);
  if (c) newRecordingTones(c);
}

/** transcription finished — a single soft high tick (D6), subtler. */
export function playTranscription(): void {
  const c = ready(300);
  if (c) tone(c, { freq: 1174.66, dur: 0.12, gain: 0.6 });
}

/** watchlist hit — a brighter three-note rising motif (E5→G#5→C6) with a
 *  triangle timbre so a match stands out from the routine new-recording
 *  chime without being alarming. */
export function playWatchAlert(): void {
  const c = ready(300);
  if (!c) return;
  tone(c, { freq: 659.25, dur: 0.12, gain: 0.9, type: "triangle" });
  tone(c, { freq: 830.61, dur: 0.12, gain: 0.85, type: "triangle", delay: 0.12 });
  tone(c, { freq: 1046.5, dur: 0.2, gain: 0.8, type: "triangle", delay: 0.24 });
}

/** confirmation when the user turns alerts on (also unlocks audio). */
export function playEnableChirp(): void {
  const c = getCtx();
  if (!c) return;
  unlockAudio();
  lastPlay = 0;
  newRecordingTones(c);
}

/** Windows-style: while dragging the volume slider, play the alert at the
 *  just-picked level so the user hears exactly how loud it'll be. */
export function playVolumePreview(v: number): void {
  setUserVolume(v);
  unlockAudio();
  const c = getCtx();
  if (!c || c.state !== "running") return;
  const now = performance.now();
  if (now - lastPreview < 130) return;
  lastPreview = now;
  newRecordingTones(c);
}
