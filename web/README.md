# Squelch — Next.js frontend

A rewrite of the Squelch web UI in **Next.js 14** (App Router, TypeScript) using
**Chakra UI v2**, **Tailwind CSS**, and **FontAwesome**. The Python/FastAPI
backend is unchanged — this app talks to its existing `/api` + `/ws`.

## Why these choices

- **Static export** (`output: 'export'`). `next build` emits a fully static
  site to `out/`, which FastAPI serves exactly like it serves `app.js` today.
  Same origin in production, so the auth cookie, `/api`, and the WebSocket all
  work with no proxy and no second runtime.
- **Chakra v2** (not v3). v3 is a ground-up API rewrite; v2 is stable and
  well-trodden, which matters because this was authored without a local Node
  toolchain to compile against. Bump later if desired.
- **Theme parity via CSS variables.** The entire original design is driven by
  CSS custom properties (`--bg`, `--accent`, `--wave-played`, …) switched by a
  `data-theme` attribute. Those are carried over verbatim in
  `src/app/globals.css`; Tailwind colors and the mono font map onto them, and
  Chakra is used only for component behavior (Modal, Menu, Tabs, Button). So
  light/dark stays pixel-identical to the original.

## Requirements

- **Node 18+** and npm (neither the dev box nor the VM had Node — install it
  wherever you build/run this).

## Develop

```bash
cd squelch/web
cp .env.example .env.local        # point BACKEND_URL / NEXT_PUBLIC_WS_BASE at FastAPI
npm install
npm run dev                       # http://localhost:3000
```

In dev, `next dev` proxies `/api` to `BACKEND_URL` (so the login cookie stays
same-origin) and the live-feed WebSocket connects to `NEXT_PUBLIC_WS_BASE`
(Next's dev rewrites can't tunnel WebSockets). Run the FastAPI backend
alongside it.

`npm run typecheck` and `npm run lint` are wired up too.

## Build & deploy

```bash
npm run build                     # emits ./out (static)
```

Then have FastAPI serve `out/`. The current backend serves
`static/index.html` at `/` and mounts `/static`; for the export it needs to
serve `out/index.html` at `/` and the hashed assets under `out/_next/…`. The
simplest change (kept in Python, per the plan):

```python
# web.py — serve the exported Next build
from fastapi.staticfiles import StaticFiles
app.mount("/", StaticFiles(directory=WEB_OUT_DIR, html=True), name="web")
# (mount this AFTER all /api and /ws routes so it only catches the frontend)
```

`html=True` makes `/` resolve to `index.html` and lets client-side routes fall
back to it. Copy/point `WEB_OUT_DIR` at `squelch/web/out`.

## Layout

```
src/
  app/            layout, providers, globals.css (the ported theme), page
  lib/            api.ts (typed client), types.ts, ws.ts (live feed), format.ts, fa.ts
  theme/          minimal Chakra theme bound to the CSS-var system
  state/          app-context (status/auth/theme/speakers + modal host state)
  components/     Header, Feed, TransmissionCard, WaveformPlayer, modals, …
```

## Port status

Backbone complete: build config, full theme, typed API client for all ~40
endpoints, WebSocket hook, state/auth/theme context, FA icon set, app shell.
The component tree (feed cards, waveform player, all dialogs, voter panel, geo
map, stats, logbook, settings) is being ported on top of this foundation.
Because it's authored without a local build, expect to shake out
import/type nits on the first `npm install && npm run build`.
