"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { fmtAgo } from "@/lib/format";
import type { AuditEntry, SessionInfo } from "@/lib/types";
import { useApp } from "@/state/app-context";

/** Enrol an authenticator app (TOTP). Shared by the Settings pane and the
 *  "you must set this up first" gate. The secret is shown as text and as an
 *  otpauth:// link (tap it on the phone that has the app) — no QR library,
 *  nothing fetched from anywhere. */
export function TwoFactorEnroll({ onDone }: { onDone: () => void | Promise<void> }) {
  const [secret, setSecret] = useState("");
  const [uri, setUri] = useState("");
  const [code, setCode] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  const begin = async () => {
    setErr("");
    try {
      const r = await api.twofaBegin();
      setSecret(r.secret);
      setUri(r.uri);
    } catch (e) {
      setErr((e as Error).message);
    }
  };

  const confirm = async () => {
    setBusy(true);
    setErr("");
    try {
      await api.twofaConfirm(code.trim());
      setSecret("");
      setCode("");
      await onDone();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  if (!secret) {
    return (
      <>
        <button className="text-btn" onClick={begin}>
          Set up an authenticator app
        </button>
        <div className="err">{err}</div>
      </>
    );
  }
  return (
    <>
      <p className="setup-intro">
        In your authenticator app (Microsoft Authenticator, Google Authenticator,
        1Password…) add an account with this key, then type the 6-digit code it
        shows.
      </p>
      <label>Setup key</label>
      <input type="text" readOnly value={secret.replace(/(.{4})/g, "$1 ").trim()}
        onFocus={(e) => e.currentTarget.select()} />
      <a className="text-btn" href={uri}>
        Open in an authenticator app on this device
      </a>
      <label>6-digit code</label>
      <input
        type="text"
        inputMode="numeric"
        autoComplete="one-time-code"
        placeholder="123456"
        value={code}
        maxLength={7}
        onChange={(e) => setCode(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") confirm();
        }}
      />
      <button className="text-btn primary" onClick={confirm} disabled={busy}>
        {busy ? "Checking…" : "Turn on two-factor"}
      </button>
      <div className="err">{err}</div>
    </>
  );
}

export function TwoFactorSection() {
  const { status, refreshStatus } = useApp();
  const [pw, setPw] = useState("");
  const [asking, setAsking] = useState(false);
  const [msg, setMsg] = useState<{ text: string; ok: boolean }>({ text: "", ok: true });
  const on = !!status?.has_2fa;

  return (
    <>
      <h4>Two-factor sign-in</h4>
      {on ? (
        <>
          <div className="user-row">
            <span className="uname">
              On
              <span className="muted-note">
                {" "}— signing in needs your password and a code from your
                authenticator app.
              </span>
            </span>
            {!asking && (
              <button className="link-danger" onClick={() => setAsking(true)}>
                turn off
              </button>
            )}
          </div>
          {asking && (
            <>
              <input
                type="password"
                placeholder="your password, to confirm"
                value={pw}
                onChange={(e) => setPw(e.target.value)}
              />
              <button
                className="text-btn"
                onClick={async () => {
                  try {
                    await api.twofaDisable(pw);
                    setPw("");
                    setAsking(false);
                    setMsg({ text: "two-factor turned off", ok: true });
                    await refreshStatus();
                  } catch (e) {
                    setMsg({ text: (e as Error).message, ok: false });
                  }
                }}
              >
                Turn off two-factor
              </button>
            </>
          )}
        </>
      ) : (
        <TwoFactorEnroll
          onDone={async () => {
            setMsg({ text: "two-factor is on — your other logins were signed out", ok: true });
            await refreshStatus();
          }}
        />
      )}
      {msg.text && <div className={msg.ok ? "ok-msg" : "err"}>{msg.text}</div>}
    </>
  );
}

function shortAgent(ua: string | null): string {
  if (!ua) return "unknown device";
  const os = /Windows|Android|iPhone|iPad|Mac OS X|Linux|CrOS/.exec(ua)?.[0] || "";
  const br = /Edg|Firefox|Chrome|Safari/.exec(ua)?.[0] || "";
  return [br === "Edg" ? "Edge" : br, os].filter(Boolean).join(" on ") || ua.slice(0, 40);
}

export function SessionsSection() {
  const [rows, setRows] = useState<SessionInfo[]>([]);
  const [msg, setMsg] = useState("");
  const load = () => api.mySessions().then((r) => setRows(r.sessions)).catch(() => {});
  useEffect(() => {
    load();
  }, []);
  const others = rows.filter((r) => !r.current).length;

  return (
    <>
      <h4>Where you&apos;re signed in</h4>
      <div>
        {rows.map((r) => (
          <div className="user-row" key={r.id}>
            <span className="uname">
              {shortAgent(r.user_agent)}
              {r.current && <span className="badge-you">this device</span>}
              <span className="muted-note">
                {" "}— {r.ip || "?"} · active {fmtAgo(r.last_seen)}
              </span>
            </span>
          </div>
        ))}
      </div>
      {others > 0 && (
        <button
          className="text-btn"
          onClick={async () => {
            try {
              const r = await api.revokeOtherSessions();
              setMsg(`${r.ended} other login(s) signed out`);
              load();
            } catch (e) {
              setMsg((e as Error).message);
            }
          }}
        >
          Sign out everywhere else
        </button>
      )}
      {msg && <div className="ok-msg">{msg}</div>}
    </>
  );
}

const ACTION_LABELS: Record<string, string> = {
  login: "signed in",
  login_failed: "failed sign-in",
  logout: "signed out",
  setup_complete: "first-run setup",
  password_changed: "changed password",
  sessions_revoked: "ended sessions",
  "2fa_enabled": "turned on two-factor",
  "2fa_disabled": "turned off two-factor",
  "2fa_reset": "reset two-factor",
  user_created: "created user",
  user_deleted: "deleted user",
  user_role_changed: "changed role",
  transmission_deleted: "deleted recording",
  transmissions_purged: "purged recordings",
  voiceprints_reset_all: "reset ALL voiceprints",
  voiceprint_reset: "reset a voiceprint",
  speakers_merged: "merged speakers",
  case_deleted: "deleted case",
  qrz_credentials_changed: "changed QRZ login",
  logo_changed: "changed logo",
  footer_changed: "changed footer",
};

/** Super-admin view of the audit trail: sign-ins, account changes and every
 *  destructive action, with who did it and from where. */
export function AuditSection() {
  const [rows, setRows] = useState<AuditEntry[] | null>(null);
  const [open, setOpen] = useState(false);
  useEffect(() => {
    if (open && rows === null) {
      api.audit(200).then((r) => setRows(r.entries)).catch(() => setRows([]));
    }
  }, [open, rows]);

  return (
    <>
      <h4>Audit log</h4>
      <button className="text-btn" onClick={() => setOpen((o) => !o)}>
        {open ? "Hide" : "Show the last 200 entries"}
      </button>
      {open && (
        <div className="audit-list">
          {rows === null && <div className="muted-note">loading…</div>}
          {rows?.length === 0 && <div className="muted-note">nothing recorded yet</div>}
          {rows?.map((r) => (
            <div className="user-row" key={r.id}>
              <span className="uname">
                {new Date(r.ts * 1000).toLocaleString()} · <b>{r.username || "anonymous"}</b>{" "}
                {ACTION_LABELS[r.action] || r.action}
                <span className="muted-note">
                  {r.detail ? ` — ${r.detail}` : ""} {r.ip ? `(${r.ip})` : ""}
                </span>
              </span>
            </div>
          ))}
        </div>
      )}
    </>
  );
}
