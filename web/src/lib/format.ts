/** Formatting + small pure helpers, ported 1:1 from app.js so output matches. */

export function fmtTime(epoch: number): string {
  return new Date(epoch * 1000).toLocaleTimeString([], {
    hour: "numeric",
    minute: "2-digit",
    second: "2-digit",
  });
}

export function fmtDay(epoch: number): string {
  return new Date(epoch * 1000).toLocaleDateString([], {
    weekday: "long",
    year: "numeric",
    month: "long",
    day: "numeric",
  });
}

export function dayKey(epoch: number): string {
  return new Date(epoch * 1000).toDateString();
}

export function dayBounds(epoch: number): [number, number] {
  const d = new Date(epoch * 1000);
  const start = new Date(d.getFullYear(), d.getMonth(), d.getDate());
  const end = new Date(start.getTime() + 86400000);
  return [start.getTime() / 1000, end.getTime() / 1000];
}

export function fmtClock(seconds: number): string {
  if (!isFinite(seconds) || seconds < 0) seconds = 0;
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}

export function speakerHue(label: string): number {
  let h = 0;
  for (const c of label) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  return h % 360;
}

export function initials(label: string): string {
  const auto = label.match(/^Speaker (\d+)$/i);
  if (auto) return "S" + auto[1];
  const words = label.trim().split(/\s+/);
  return words
    .slice(0, 2)
    .map((w) => w[0])
    .join("")
    .toUpperCase();
}

export function fmtAirtime(ms: number): string {
  const s = Math.round((ms || 0) / 1000);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m ${sec}s`;
  return `${sec}s`;
}

export function fmtAgo(ts: number): string {
  const s = Date.now() / 1000 - ts;
  if (s < 90) return "just now";
  if (s < 3600) return Math.round(s / 60) + "m ago";
  if (s < 86400) return Math.round(s / 3600) + "h ago";
  if (s < 86400 * 30) return Math.round(s / 86400) + "d ago";
  return new Date(ts * 1000).toLocaleDateString();
}

/** number of lit signal bars (of 4) for an SNR estimate. */
export function signalBars(snr: number): number {
  return snr >= 26 ? 4 : snr >= 16 ? 3 : snr >= 8 ? 2 : 1;
}

export function dateToEpoch(str: string): number {
  return new Date(str + "T00:00:00").getTime() / 1000;
}

export function epochToDateInput(epoch: number): string {
  const d = new Date(epoch * 1000);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

// <input type="datetime-local"> uses local time with no zone suffix
export function datetimeToEpoch(str: string): number {
  return new Date(str).getTime() / 1000;
}

export function epochToDatetimeInput(epoch: number): string {
  const d = new Date(epoch * 1000);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
}

/** avatar background/foreground colors for a speaker label. */
export function avatarColors(label: string): { bg: string; color: string } {
  const hue = speakerHue(label);
  return {
    bg: `hsla(${hue}, 60%, 55%, .18)`,
    color: `hsl(${hue}, 65%, 72%)`,
  };
}

export function nameColor(label: string): string {
  return `hsl(${speakerHue(label)}, 65%, 75%)`;
}

export const DOW = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
export const THEME_LABELS: Record<string, string> = {
  night: "Night Mode",
  day: "Day Mode",
  crt: "Green (VT220)",
  amber: "Amber (Wyse 60)",
  cyan: "Cyan (ADM-3A)",
  borland: "Borland Blue (DOS)",
  c64: "Commodore 64",
  paper: "Paper White (DEC P4)",
};
export const THEME_KEY = "squelch-theme";

/** Phosphor CRT themes get the scanline/glow/scope treatment. */
export const PHOSPHOR_THEMES = new Set(["crt", "amber", "cyan"]);
export function isPhosphor(theme: string | null | undefined): boolean {
  return !!theme && PHOSPHOR_THEMES.has(theme);
}
