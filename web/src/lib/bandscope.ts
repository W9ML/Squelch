// Bandscope columns arrive ~30/sec — far too fast for React state. The Feed's
// WS handler decodes each `bscope` event here and fans the raw byte column out
// to whichever <Bandscope> canvas is mounted, which draws it imperatively.

type Column = Uint8Array;
const subs = new Set<(c: Column) => void>();

/** Decode a base64 `bscope` column (128 log-mag bytes) and push to the canvas. */
export function pushBscope(b64: string): void {
  if (!subs.size || !b64) return;
  let bin: string;
  try {
    bin = atob(b64);
  } catch {
    return;
  }
  const col = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) col[i] = bin.charCodeAt(i);
  subs.forEach((fn) => fn(col));
}

export function subscribeBscope(fn: (c: Column) => void): () => void {
  subs.add(fn);
  return () => subs.delete(fn);
}
