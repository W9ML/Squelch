import { extendTheme, type ThemeConfig } from "@chakra-ui/react";

/**
 * Deliberately minimal Chakra theme. The visual design lives in globals.css
 * (the CSS custom-property system + `data-theme` switching), so Chakra is used
 * for component behavior — Modal, Menu, Tabs, Button, Input — not for colors.
 *
 * We therefore:
 *  - blank out `styles.global` so Chakra never paints the <body> (globals.css
 *    owns the gradient background), and pair this with `resetCSS={false}` on
 *    the provider (see providers.tsx).
 *  - keep the Inter/mono font stack in sync with the original.
 */
const config: ThemeConfig = {
  initialColorMode: "dark",
  useSystemColorMode: false,
};

export const theme = extendTheme({
  config,
  styles: {
    global: () => ({}),
  },
  fonts: {
    heading: `'Inter', 'Segoe UI', system-ui, -apple-system, sans-serif`,
    body: `'Inter', 'Segoe UI', system-ui, -apple-system, sans-serif`,
    mono: `var(--mono)`,
  },
});
