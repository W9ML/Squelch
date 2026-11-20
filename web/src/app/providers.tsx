"use client";

import "@/lib/fa";
import { useState, type ReactNode } from "react";
import createCache from "@emotion/cache";
import { CacheProvider } from "@emotion/react";
import { ChakraProvider } from "@chakra-ui/react";
import { useServerInsertedHTML } from "next/navigation";
import { theme } from "@/theme";
import { AppProvider } from "@/state/app-context";

/** Collects Chakra/Emotion's generated CSS during the build-time prerender and
 *  emits it once through Next's server-inserted HTML, instead of letting
 *  Emotion drop `<style>` tags in between the app's elements. Those stray tags
 *  are not part of the React tree, so React 19 intermittently failed hydration
 *  on them (minified error #418) and re-rendered the whole page client-side. */
function EmotionRegistry({ children }: { children: ReactNode }) {
  const [{ cache, flush }] = useState(() => {
    const cache = createCache({ key: "css" });
    cache.compat = true;
    const prevInsert = cache.insert;
    let inserted: string[] = [];
    cache.insert = (...args) => {
      const serialized = args[1];
      if (cache.inserted[serialized.name] === undefined) inserted.push(serialized.name);
      return prevInsert(...args);
    };
    const flush = () => {
      const names = inserted;
      inserted = [];
      return names;
    };
    return { cache, flush };
  });

  useServerInsertedHTML(() => {
    const names = flush();
    if (names.length === 0) return null;
    let styles = "";
    for (const name of names) {
      const css = cache.inserted[name];
      if (typeof css === "string") styles += css;
    }
    return (
      <style
        key={cache.key}
        data-emotion={`${cache.key} ${names.join(" ")}`}
        dangerouslySetInnerHTML={{ __html: styles }}
      />
    );
  });

  return <CacheProvider value={cache}>{children}</CacheProvider>;
}

export function Providers({ children }: { children: ReactNode }) {
  // resetCSS={false}: globals.css owns the reset + <body> background so Chakra
  // doesn't paint over the themed gradient (see theme/index.ts).
  return (
    <EmotionRegistry>
      <ChakraProvider theme={theme} resetCSS={false}>
        <AppProvider>{children}</AppProvider>
      </ChakraProvider>
    </EmotionRegistry>
  );
}
