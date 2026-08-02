"use client";

import { useState } from "react";
import { api } from "@/lib/api";
import { useApp } from "@/state/app-context";
import { SqModal } from "./SqModal";

export function RenameSpeakerModal({ speakerId, label }: { speakerId: number; label: string }) {
  const { closeModal } = useApp();
  const [name, setName] = useState(label);
  const [err, setErr] = useState("");

  const save = async () => {
    try {
      await api.renameSpeaker(speakerId, name.trim());
      closeModal();
    } catch (e) {
      setErr((e as Error).message);
    }
  };

  return (
    <SqModal
      title="Rename / merge speaker"
      onClose={closeModal}
      footer={
        <>
          <button className="text-btn" onClick={closeModal}>
            Cancel
          </button>
          <button className="text-btn primary" onClick={save}>
            Save
          </button>
        </>
      }
    >
      <p className="muted">
        {`Rename “${label}” everywhere. Renaming to a name another speaker already has merges the two.`}
      </p>
      <input type="text" value={name} autoFocus onChange={(e) => setName(e.target.value)} />
      <div className="err">{err}</div>
    </SqModal>
  );
}
