"use client";

import { useState } from "react";
import { api } from "@/lib/api";
import { useApp } from "@/state/app-context";
import { SqModal } from "./SqModal";

/** First-run gate (rendered while status.needs_setup): create the first admin
 *  account + optional station identity. The /api/setup endpoint self-closes the
 *  moment any user exists, so it can't be used to add accounts later. */
export function SetupModal() {
  const { refreshStatus } = useApp();
  const [code, setCode] = useState("");
  const [user, setUser] = useState("");
  const [pass, setPass] = useState("");
  const [confirm, setConfirm] = useState("");
  const [callsign, setCallsign] = useState("");
  const [node, setNode] = useState("");
  const [footer, setFooter] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    if (!code.trim()) return setErr("enter the setup code from the server");
    if (!user.trim()) return setErr("choose an admin username");
    if (pass.length < 12) return setErr("password must be at least 12 characters");
    if (pass !== confirm) return setErr("the passwords do not match");
    setErr("");
    setBusy(true);
    try {
      await api.setup({
        setup_code: code.trim(),
        username: user.trim(),
        password: pass,
        callsign: callsign.trim(),
        node: node.trim(),
        footer: footer.trim(),
      });
      await refreshStatus(); // needs_setup flips false -> this gate lifts
    } catch (e) {
      setErr((e as Error).message);
      setBusy(false);
    }
  };

  return (
    <SqModal
      title="Welcome to Squelch"
      noClose
      onClose={() => {}}
      footer={
        <button className="text-btn primary" onClick={submit} disabled={busy}>
          {busy ? "Setting up…" : "Complete setup"}
        </button>
      }
    >
      <p className="setup-intro">
        Create the administrator account. To prove this is your server, enter
        the one-time setup code it printed when it started — run{" "}
        <code>sudo cat /var/lib/squelch/setup-code</code> on the server (it is
        also in <code>journalctl -u squelch</code>).
      </p>

      <label>Setup code</label>
      <input
        type="text"
        placeholder="XXXX-XXXX"
        value={code}
        autoFocus
        autoCapitalize="characters"
        autoComplete="off"
        spellCheck={false}
        onChange={(e) => setCode(e.target.value)}
      />
      <label>Admin username</label>
      <input
        type="text"
        placeholder="admin"
        value={user}
        onChange={(e) => setUser(e.target.value)}
      />
      <label>
        Password <span className="hint">(at least 12 characters)</span>
      </label>
      <input
        type="password"
        placeholder="password"
        value={pass}
        onChange={(e) => setPass(e.target.value)}
      />
      <label>Confirm password</label>
      <input
        type="password"
        placeholder="repeat password"
        value={confirm}
        onChange={(e) => setConfirm(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") submit();
        }}
      />

      <div className="setup-optional">
        <span className="setup-sep">Station identity — optional</span>
        <label>Callsign</label>
        <input
          type="text"
          placeholder="e.g. W9ML"
          value={callsign}
          onChange={(e) => setCallsign(e.target.value)}
        />
        <label>Node number</label>
        <input
          type="text"
          placeholder="e.g. 12345"
          value={node}
          onChange={(e) => setNode(e.target.value)}
        />
        <label>Footer text</label>
        <input
          type="text"
          placeholder="shown at the bottom of the page"
          value={footer}
          onChange={(e) => setFooter(e.target.value)}
        />
      </div>

      <div className="err">{err}</div>
    </SqModal>
  );
}
