"use client";

import { useEffect, useRef } from "react";
import type { Word } from "@/lib/types";

/** Word-synced transcript. Highlights the current word off the player's
 *  `ptime` events and seeks on click via `seekto` — imperative class toggles
 *  (like the original) so playback doesn't re-render the whole card. */
export function Karaoke({ words, bus }: { words: Word[]; bus: EventTarget }) {
  const spanRefs = useRef<(HTMLSpanElement | null)[]>([]);
  const activeRef = useRef(-1);

  useEffect(() => {
    const clear = () => {
      const prev = activeRef.current;
      if (prev >= 0) spanRefs.current[prev]?.classList.remove("kw-on");
      activeRef.current = -1;
    };
    const onTime = (e: Event) => {
      const t = (e as CustomEvent<number>).detail;
      let idx = -1;
      for (let i = 0; i < words.length; i++) {
        if (t >= words[i][1] && t < words[i][2] + 0.05) {
          idx = i;
          break;
        }
        if (t >= words[i][1]) idx = i;
      }
      if (idx !== activeRef.current) {
        const prev = activeRef.current;
        if (prev >= 0) spanRefs.current[prev]?.classList.remove("kw-on");
        if (idx >= 0) spanRefs.current[idx]?.classList.add("kw-on");
        activeRef.current = idx;
      }
    };
    bus.addEventListener("ptime", onTime);
    bus.addEventListener("pended", clear);
    return () => {
      bus.removeEventListener("ptime", onTime);
      bus.removeEventListener("pended", clear);
    };
  }, [words, bus]);

  return (
    <div className="transcript karaoke">
      {words.map((w, i) => (
        <span
          key={i}
          className="kw"
          ref={(el) => {
            spanRefs.current[i] = el;
          }}
          onClick={() => bus.dispatchEvent(new CustomEvent("seekto", { detail: w[1] }))}
        >
          {w[0] + " "}
        </span>
      ))}
    </div>
  );
}
