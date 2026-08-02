"use client";

/**
 * Cross-card playback coordination. Each WaveformPlayer lives in its own card
 * component, so they hand off through a shared event bus: a keyboard shortcut
 * (or another card) can start a given recording.
 *
 * Events on playBus:
 *   "play" detail=txId — the player owning txId should start
 */
export const playBus = new EventTarget();

export function requestPlay(txId: number): void {
  playBus.dispatchEvent(new CustomEvent("play", { detail: txId }));
}
