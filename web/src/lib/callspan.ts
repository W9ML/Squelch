/** Map a heard callsign back to its audio span in the word-timestamps, so a
 *  chip can replay exactly those ~1.5 seconds ("say again"). Mirrors the
 *  backend extractor's normalization: phonetic words -> their letter, digit
 *  words -> their digit, everything else -> its alnum chars. */

import type { Word } from "./types";

const PHON: Record<string, string> = {
  alpha: "A", alfa: "A", bravo: "B", charlie: "C", delta: "D", echo: "E",
  foxtrot: "F", golf: "G", hotel: "H", india: "I", juliet: "J", juliett: "J",
  kilo: "K", lima: "L", mike: "M", november: "N", oscar: "O", papa: "P",
  quebec: "Q", romeo: "R", sierra: "S", tango: "T", uniform: "U", victor: "V",
  whiskey: "W", whisky: "W", xray: "X", yankee: "Y", zulu: "Z",
};
const DIG: Record<string, string> = {
  zero: "0", one: "1", two: "2", three: "3", four: "4", five: "5",
  six: "6", seven: "7", eight: "8", nine: "9", niner: "9",
};

function wordChars(w: string): string {
  const low = w.toLowerCase().replace(/[^a-z0-9]/g, "");
  if (PHON[low]) return PHON[low];
  if (DIG[low]) return DIG[low];
  return w.toUpperCase().replace(/[^A-Z0-9]/g, "");
}

/** Smallest run of consecutive words whose normalized chars contain `call`.
 *  Returns [startSec, endSec] or null. */
export function findCallSpan(words: Word[] | null | undefined, call: string): [number, number] | null {
  if (!words || !words.length || !call) return null;
  const C = call.toUpperCase();
  const chars = words.map((w) => wordChars(w[0]));
  for (let i = 0; i < words.length; i++) {
    let acc = "";
    for (let j = i; j < words.length && j < i + 6; j++) {
      acc += chars[j];
      if (acc.includes(C)) return [words[i][1], words[j][2]];
      if (acc.length > C.length + 3) break; // this window can't be the call
    }
  }
  return null;
}
