"use client";

import { Header } from "@/components/Header";
import { Feed } from "@/components/Feed";
import { ModalHost } from "@/components/ModalHost";
import { useEffect, useState } from "react";
import { useApp } from "@/state/app-context";

export default function Page() {
  const { status, locked } = useApp();
  // The prebuilt HTML holds the "connecting" placeholder. The status call can
  // come back before React has hydrated this part of the page, so hold the
  // placeholder until we're mounted — otherwise the first client render
  // wouldn't match the HTML it is hydrating (React error #418).
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);
  // Private mode ([web] require_login), not signed in yet: draw nothing but
  // the sign-in door. The feed, its WebSocket and the live-audio controls
  // aren't mounted at all, so nothing asks the server for data it would refuse.
  // Until the first status arrives we don't know which mode this is, so wait
  // for it rather than flash (and fetch) a feed that may be private.
  if (!mounted || !status) {
    return <header className="locked-brand">Squelch <small>connecting…</small></header>;
  }
  if (locked) {
    return (
      <>
        <header className="locked-brand">{status.site_name || "Squelch"}</header>
        <ModalHost />
      </>
    );
  }
  return (
    <>
      <Header />
      <Feed />
      <ModalHost />
    </>
  );
}
