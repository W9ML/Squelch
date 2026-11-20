"use client";

import { useRef, useState } from "react";
import { api, ApiError } from "@/lib/api";
import { useApp } from "@/state/app-context";
import { SqModal } from "./SqModal";

/** Sign-in. `gate` is the private-mode door: it can't be dismissed, because
 *  there is nothing behind it to go back to until you're signed in. */
export function LoginModal({ gate = false }: { gate?: boolean }) {
  const { closeModal, refreshStatus } = useApp();
  const [user, setUser] = useState("");
  const [pass, setPass] = useState("");
  const [code, setCode] = useState("");
  const [needCode, setNeedCode] = useState(false);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const passRef = useRef<HTMLInputElement>(null);
  const codeRef = useRef<HTMLInputElement>(null);

  const submit = async () => {
    setBusy(true);
    try {
      await api.login(user.trim(), pass, code.trim());
      if (!gate) closeModal();
      await refreshStatus();
    } catch (e) {
      if (e instanceof ApiError && e.data.totp_required) {
        // right password; this account also needs its authenticator code
        setNeedCode(true);
        setErr("");
        setTimeout(() => codeRef.current?.focus(), 0);
      } else {
        setErr((e as Error).message);
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <SqModal
      title={gate ? "Sign in" : "Log in"}
      noClose={gate}
      onClose={gate ? () => {} : closeModal}
      footer={
        <>
          {!gate && (
            <button className="text-btn" onClick={closeModal}>
              Cancel
            </button>
          )}
          <button className="text-btn primary" onClick={submit} disabled={busy}>
            Log in
          </button>
        </>
      }
    >
      {gate && (
        <p className="setup-intro">This site is private. Sign in to continue.</p>
      )}
      <label>Username</label>
      <input
        type="text"
        placeholder="username"
        autoComplete="username"
        value={user}
        autoFocus
        onChange={(e) => setUser(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") passRef.current?.focus();
        }}
      />
      <label>Password</label>
      <input
        ref={passRef}
        type="password"
        placeholder="password"
        autoComplete="current-password"
        value={pass}
        onChange={(e) => setPass(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") submit();
        }}
      />
      {needCode && (
        <>
          <label>Authenticator code</label>
          <input
            ref={codeRef}
            type="text"
            inputMode="numeric"
            autoComplete="one-time-code"
            placeholder="6-digit code"
            maxLength={7}
            value={code}
            onChange={(e) => setCode(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") submit();
            }}
          />
        </>
      )}
      <div className="err">{err}</div>
    </SqModal>
  );
}
