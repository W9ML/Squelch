"use client";

/** True on phones/tablets (coarse primary pointer, no hover). Used to drop
 *  the in-app volume sliders there: the OS media volume already scales
 *  everything the browser plays, and stacking a second attenuator on top is
 *  the classic "why is it quiet at full phone volume" trap. */
export function isCoarsePointer(): boolean {
  if (typeof window === "undefined" || !window.matchMedia) return false;
  return window.matchMedia("(hover: none), (pointer: coarse)").matches;
}
