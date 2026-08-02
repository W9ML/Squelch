import type { Config } from "tailwindcss";

/**
 * Tailwind is used for layout utilities only. Two deliberate choices let it
 * coexist with Chakra UI:
 *
 *  1. `preflight: false` — Tailwind's CSS reset is disabled so it doesn't fight
 *     Chakra's own reset (CSSReset). Base element styling lives in globals.css.
 *  2. Colors map onto the app's CSS custom properties (the same `--bg`,
 *     `--accent`, … that drive the ported light/dark themes), so a Tailwind
 *     class like `text-accent` and a Chakra `color="accent"` resolve to the
 *     exact same value and both follow `data-theme`.
 */
const config: Config = {
  content: [
    "./src/app/**/*.{ts,tsx}",
    "./src/components/**/*.{ts,tsx}",
    "./src/**/*.{ts,tsx}",
  ],
  corePlugins: { preflight: false },
  theme: {
    extend: {
      colors: {
        bg: "var(--bg)",
        panel: "var(--panel)",
        card: "var(--card)",
        "card-hover": "var(--card-hover)",
        border: "var(--border)",
        "border-soft": "var(--border-soft)",
        text: "var(--text)",
        "text-dim": "var(--text-dim)",
        "text-faint": "var(--text-faint)",
        accent: "var(--accent)",
        "accent-ink": "var(--accent-ink)",
        "accent-soft": "var(--accent-soft)",
        green: "var(--green)",
        red: "var(--red)",
        amber: "var(--amber)",
        blue: "var(--blue)",
      },
      fontFamily: {
        mono: "var(--mono)",
      },
      borderRadius: {
        card: "var(--radius)",
      },
      maxWidth: {
        feed: "880px",
      },
    },
  },
  plugins: [],
};

export default config;
