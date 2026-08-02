"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import type { SpeakerFacet } from "@/lib/types";
import { useApp } from "@/state/app-context";
import { SqModal } from "./SqModal";

export function MdcLinkModal({ unit, currentOp }: { unit: string; currentOp: string | null }) {
  const { closeModal } = useApp();
  const [speakerId, setSpeakerId] = useState("");
  const [named, setNamed] = useState<SpeakerFacet[]>([]);
  const [err, setErr] = useState("");

  useEffect(() => {
    api
      .speakers()
      .then(({ speakers }) =>
        setNamed(speakers.filter((s) => s.is_named).sort((a, b) => a.label.localeCompare(b.label))),
      )
      .catch(() => {});
  }, []);

  const link = async () => {
    if (!speakerId) {
      setErr("Pick an operator.");
      return;
    }
    try {
      await api.linkMdcUnit(unit, Number(speakerId));
      closeModal();
    } catch (e) {
      setErr((e as Error).message);
    }
  };
  const unlink = async () => {
    try {
      await api.unlinkMdcUnit(unit);
      closeModal();
    } catch (e) {
      setErr((e as Error).message);
    }
  };

  return (
    <SqModal
      title={`MDC unit ${unit}`}
      onClose={closeModal}
      footer={
        <>
          <button className="text-btn" onClick={closeModal}>
            Cancel
          </button>
          {currentOp && (
            <button className="text-btn" onClick={unlink}>
              Unlink
            </button>
          )}
          <button className="text-btn primary" onClick={link}>
            Link
          </button>
        </>
      }
    >
      <p className="muted">
        {currentOp
          ? `Unit ${unit} is linked to ${currentOp}. Pick a different operator, or unlink it.`
          : `Link unit ${unit} to the operator whose radio sends it. Every over from this unit is then attributed to them automatically.`}
      </p>
      <label>Operator</label>
      <select value={speakerId} onChange={(e) => setSpeakerId(e.target.value)}>
        <option value="">— choose operator —</option>
        {named.map((s) => (
          <option key={s.id} value={String(s.id)}>
            {s.label}
          </option>
        ))}
      </select>
      <div className="err">{err}</div>
    </SqModal>
  );
}
