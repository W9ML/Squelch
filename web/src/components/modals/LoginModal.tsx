"use client";

import { useRef, useState } from "react";
import { api } from "@/lib/api";
import { useApp } from "@/state/app-context";
import { SqModal } from "./SqModal";

export function LoginModal() {
  const { closeModal, refreshStatus } = useApp();
  const [user, setUser] = useState("");
  const [pass, setPass] = useState("");
  const [err, setErr] = useState("");
  const passRef = useRef<HTMLInputElement>(null);

  const submit = async () => {
    try {
      await api.login(user.trim(), pass);
      closeModal();
      await refreshStatus();
    } catch (e) {
      setErr((e as Error).message);
    }
  };

  return (
    <SqModal
      title="Admin login"
      onClose={closeModal}
      footer={
        <>
          <button className="text-btn" onClick={closeModal}>
            Cancel
          </button>
          <button className="text-btn primary" onClick={submit}>
            Log in
          </button>
        </>
      }
    >
      <label>Username</label>
      <input
        type="text"
        placeholder="username"
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
        value={pass}
        onChange={(e) => setPass(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") submit();
        }}
      />
      <div className="err">{err}</div>
    </SqModal>
  );
}
