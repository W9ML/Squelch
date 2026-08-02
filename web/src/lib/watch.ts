import type { Watch } from "./types";

/** Human-friendly description of a watch rule, shared by the alerts menu and
 *  the settings management list. Speaker watches carry the operator name in
 *  `label`; callsign/unit watches read from `value`. */
export function watchLabel(w: Watch): string {
  switch (w.kind) {
    case "emergency":
      return "any MDC EMERGENCY";
    case "speaker":
      return w.label || `Speaker ${w.value}`;
    case "mdc_unit":
      return `MDC unit ${w.value}`;
    case "callsign":
      return `“${w.value}” mentioned`;
    default:
      return w.label || w.value;
  }
}
