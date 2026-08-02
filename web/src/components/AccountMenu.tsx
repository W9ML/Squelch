"use client";

import { useEffect, useRef, useState } from "react";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { api } from "@/lib/api";
import { useApp } from "@/state/app-context";
import { ICONS } from "./icons";

/** The account tile (top-right when logged in). Clicking opens a small menu
 *  with Settings and Log Out (the latter confirms first) — no more accidental
 *  one-click logout. */
export function AccountMenu() {
  const { status, canSettings, openModal } = useApp();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDoc = (e: PointerEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("pointerdown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("pointerdown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const name = status?.username || "?";
  const initials = name.replace(/[^a-z0-9]/gi, "").slice(0, 2).toUpperCase();
  const hasPhoto = !!status?.avatar_ts;

  return (
    <div className="acct-wrap" ref={ref}>
      <button
        className={"text-btn login-btn acct" + (hasPhoto ? " has-photo" : "")}
        title={status?.username || "Account"}
        aria-label="Account menu"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
      >
        {hasPhoto ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img src={api.avatarUrl(status!.avatar_ts!)} alt={name} />
        ) : (
          initials || "?"
        )}
      </button>

      {open && (
        <div className="acct-menu" role="menu">
          <div className="acct-name">{status?.username}</div>
          {status?.has_timemachine && (
            <button
              className="acct-item"
              role="menuitem"
              onClick={() => {
                setOpen(false);
                openModal({ kind: "timemachine" });
              }}
            >
              <FontAwesomeIcon icon={ICONS.calendar} /> Time Machine
            </button>
          )}
          <button
            className="acct-item"
            role="menuitem"
            onClick={() => {
              setOpen(false);
              openModal({ kind: "settings" });
            }}
          >
            <FontAwesomeIcon icon={ICONS.settings} /> Settings
          </button>
          {canSettings && (
            <button
              className="acct-item"
              role="menuitem"
              onClick={() => {
                setOpen(false);
                openModal({ kind: "connlog" });
              }}
            >
              <FontAwesomeIcon icon={ICONS.log} /> Log
            </button>
          )}
          {canSettings && status?.has_voter && (
            <button
              className="acct-item"
              role="menuitem"
              onClick={() => {
                setOpen(false);
                window.location.assign(api.voterStatusUrl());
              }}
            >
              <FontAwesomeIcon icon={ICONS.voter} /> Voter Status
            </button>
          )}
          <button
            className="acct-item danger"
            role="menuitem"
            onClick={() => {
              setOpen(false);
              openModal({ kind: "logout" });
            }}
          >
            <FontAwesomeIcon icon={ICONS.logout} /> Log Out
          </button>
        </div>
      )}
    </div>
  );
}
