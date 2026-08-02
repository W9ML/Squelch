"use client";

import "@/lib/fa";
import { ChakraProvider } from "@chakra-ui/react";
import { theme } from "@/theme";
import { AppProvider } from "@/state/app-context";
import type { ReactNode } from "react";

export function Providers({ children }: { children: ReactNode }) {
  // resetCSS={false}: globals.css owns the reset + <body> background so Chakra
  // doesn't paint over the themed gradient (see theme/index.ts).
  return (
    <ChakraProvider theme={theme} resetCSS={false}>
      <AppProvider>{children}</AppProvider>
    </ChakraProvider>
  );
}
