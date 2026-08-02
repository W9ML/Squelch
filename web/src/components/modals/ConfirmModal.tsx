"use client";

import { useState } from "react";
import { useApp, type Modal } from "@/state/app-context";
import { SqModal } from "./SqModal";

/** Generic in-app confirmation dialog. Driven by the modal system so callers
 *  just openModal({ kind: "confirm", ... }) with an onConfirm callback. */
export function ConfirmModal({ m }: { m: Extract<Modal, { kind: "confirm" }> }) {
  const { closeModal } = useApp();
  const [busy, setBusy] = useState(false);

  const go = async () => {
    setBusy(true);
    try {
      await m.onConfirm();
      closeModal();
    } catch {
      setBusy(false);
    }
  };

  return (
    <SqModal
      title={m.title}
      onClose={closeModal}
      footer={
        <>
          <button className="text-btn" onClick={closeModal}>
            Cancel
          </button>
          <button
            className={"text-btn" + (m.danger ? " danger" : "")}
            disabled={busy}
            onClick={go}
          >
            {m.confirmLabel || "Confirm"}
          </button>
        </>
      }
    >
      <p className="muted">{m.message}</p>
    </SqModal>
  );
}
