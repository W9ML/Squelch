"use client";

import { useState } from "react";
import { api } from "@/lib/api";
import { useApp } from "@/state/app-context";
import { SqModal } from "./SqModal";

export function LogoutModal() {
  const { closeModal, refreshStatus, username } = useApp();
  const [busy, setBusy] = useState(false);

  const logout = async () => {
    setBusy(true);
    try {
      await api.logout();
      await refreshStatus();
      closeModal();
    } catch {
      setBusy(false);
    }
  };

  return (
    <SqModal
      title="Log out?"
      onClose={closeModal}
      footer={
        <>
          <button className="text-btn" onClick={closeModal}>
            Cancel
          </button>
          <button className="text-btn danger" disabled={busy} onClick={logout}>
            Log out
          </button>
        </>
      }
    >
      <p className="muted">
        {username ? `Log out of Squelch as ${username}?` : "Log out of Squelch?"}
      </p>
    </SqModal>
  );
}
