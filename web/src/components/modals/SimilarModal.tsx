"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { fmtTime } from "@/lib/format";
import type { SimilarRow, Transmission } from "@/lib/types";
import { useApp } from "@/state/app-context";
import { SqModal } from "./SqModal";

export function SimilarModal({ tx }: { tx: Transmission }) {
  const { closeModal, setFilter } = useApp();
  const [rows, setRows] = useState<SimilarRow[] | null>(null);
  const [msg, setMsg] = useState("searching…");

  useEffect(() => {
    api
      .similar(tx.id)
      .then(({ transmissions }) => {
        setRows(transmissions);
        if (!transmissions.length) setMsg("no other voice-printed transmissions yet.");
      })
      .catch((e) => setMsg((e as Error).message));
  }, [tx.id]);

  return (
    <SqModal
      title="Similar-sounding transmissions"
      onClose={closeModal}
      footer={
        <button className="text-btn primary" onClick={closeModal}>
          Close
        </button>
      }
    >
      <p className="muted">
        Ranked by voice-print similarity. Radio audio is fuzzy — treat these as possible matches, not
        proof.
      </p>
      <div className="similar-list">
        {!rows || rows.length === 0
          ? msg
          : rows.slice(0, 15).map((r) => (
              <div
                className="similar-row"
                key={r.id}
                style={r.speaker_id == null ? { cursor: "default" } : undefined}
                onClick={() => {
                  if (r.speaker_id != null) {
                    closeModal();
                    setFilter("speaker_id", r.speaker_id);
                  }
                }}
              >
                <span className="sim-pct">{Math.round(r.similarity * 100)}%</span>
                <span className="sim-spk">{r.speaker_label || "unknown voice"}</span>
                <span className="sim-tx">{(r.transcript || "").slice(0, 48) || "—"}</span>
                <span className="sim-time">{fmtTime(r.started_at)}</span>
              </div>
            ))}
      </div>
    </SqModal>
  );
}
