/**
 * Squelch frontend build config.
 *
 * Production: `next build` emits a fully static site to `out/` (output:
 * 'export'). FastAPI serves that directory exactly like it serves the old
 * app.js today, so the browser talks to /api and /ws on the SAME origin —
 * cookies and the WebSocket just work, no proxy, no second runtime.
 *
 * Development: `next dev` proxies /api to the FastAPI backend (BACKEND_URL,
 * default 127.0.0.1:8080) so the auth cookie stays same-origin while you get
 * HMR. The WebSocket in dev connects straight to NEXT_PUBLIC_WS_BASE (see
 * .env.example) because Next's dev rewrites don't tunnel WebSockets.
 */
const isDev = process.env.NODE_ENV === "development";
const backend = process.env.BACKEND_URL || "http://127.0.0.1:8080";

/** @type {import('next').NextConfig} */
const nextConfig = {
  output: "export",
  reactStrictMode: true,
  // static export can't optimize images at runtime
  images: { unoptimized: true },
  // rewrites only apply to `next dev`; they're ignored by the export build
  // (prod is same-origin), so gate them to dev to keep the build clean.
  ...(isDev && {
    async rewrites() {
      return [{ source: "/api/:path*", destination: `${backend}/api/:path*` }];
    },
  }),
};

export default nextConfig;
