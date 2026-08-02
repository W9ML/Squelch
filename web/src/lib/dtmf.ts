/** AllStar DTMF command interpretation.
 *
 *  Function codes are node-configurable (rpt.conf [functions]), so this
 *  table covers the stock app_rpt/ASL defaults and anything else falls
 *  back to a generic "Command *NN" label. Interpretation is display-only
 *  — the raw digits are always shown/stored. */

export interface DtmfCmd {
  raw: string;
  label: string | null;
}

/** [code, label, takes an argument] — longest codes first so "*70"
 *  doesn't parse as "*7 0". */
const FUNCS: [string, string, boolean][] = [
  ["70", "Link status", false],
  ["71", "Disconnect all links", false],
  ["1", "Disconnect", true],
  ["2", "Monitor", true],
  ["3", "Connect", true],
  ["4", "Command mode", true],
  ["6", "Autopatch", true],
];

function interp(body: string, raw: string): DtmfCmd {
  for (const [code, label, hasArg] of FUNCS) {
    if (body.startsWith(code)) {
      const arg = body.slice(code.length);
      if (hasArg) return { raw, label: arg ? `${label} ${arg}` : `${label}…` };
      if (!arg) return { raw, label };
      break; // a no-arg code followed by digits: not this command
    }
  }
  return { raw, label: body ? `Command *${body}` : null };
}

/** Split a press sequence into command segments. '*' starts a command;
 *  '#' terminates one (patch down / entry abort on most nodes). */
export function parseDtmf(seq: string): DtmfCmd[] {
  const out: DtmfCmd[] = [];
  let i = 0;
  while (i < seq.length) {
    if (seq[i] === "*") {
      let j = i + 1;
      while (j < seq.length && seq[j] !== "*" && seq[j] !== "#") j++;
      out.push(interp(seq.slice(i + 1, j), seq.slice(i, j)));
      if (j < seq.length && seq[j] === "#") j++;
      i = j;
    } else {
      let j = i;
      while (j < seq.length && seq[j] !== "*") j++;
      out.push({ raw: seq.slice(i, j), label: null });
      i = j;
    }
  }
  return out;
}

/** One-line human summary of a press sequence ("Connect 46655 · 123"). */
export function dtmfSummary(seq: string): string {
  return parseDtmf(seq)
    .map((c) => c.label || c.raw)
    .join(" · ");
}
