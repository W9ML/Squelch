"use client";

import { dtmfSummary } from "@/lib/dtmf";

const KEYS = ["1", "2", "3", "A", "4", "5", "6", "B", "7", "8", "9", "C", "*", "0", "#", "D"];

/** Live DTMF keypad: appears while control tones are coming in, glows the
 *  key being pressed, and interprets the sequence as AllStar commands. */
export function DtmfOverlay({
  digits,
  flash,
  nonce,
}: {
  digits: string;
  flash: string;
  nonce: number;
}) {
  const label = dtmfSummary(digits);
  return (
    <div id="dtmf-overlay" role="status" aria-live="polite">
      <div className="dt-title">DTMF</div>
      <div className="dt-grid">
        {KEYS.map((k) => (
          // keying the lit cell on the press nonce restarts the glow
          // animation when the same digit is hit twice in a row
          <span
            key={k === flash ? `${k}:${nonce}` : k}
            className={"dt-key" + (k === flash ? " lit" : "")}
          >
            {k}
          </span>
        ))}
      </div>
      <div className="dt-seq">{digits}</div>
      {label && label !== digits && <div className="dt-label">{label}</div>}
    </div>
  );
}
