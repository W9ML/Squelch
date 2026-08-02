"use client";

import { useState } from "react";
import { api } from "@/lib/api";
import type { Transmission } from "@/lib/types";
import { useApp } from "@/state/app-context";
import { SqModal } from "./SqModal";

export function SpeakerModal({ tx }: { tx: Transmission }) {
  const { closeModal, speakers } = useApp();
  const current = tx.speaker_id != null ? speakers.get(tx.speaker_id) || tx.speaker_label : null;
  const [name, setName] = useState(current && tx.speaker_named ? current : "");
  const [err, setErr] = useState("");
  const [rmsg, setRmsg] = useState<{ text: string; ok: boolean }>({ text: "", ok: true });
  const [confirmReset, setConfirmReset] = useState(false);

  const rebuild = async () => {
    setRmsg({ text: "rebuilding…", ok: true });
    try {
      const r = await api.rebuildVoiceprint(tx.speaker_id!);
      setRmsg({
        text: r.samples
          ? `Rebuilt from ${r.samples} recording${r.samples === 1 ? "" : "s"}.`
          : "No voice samples assigned — print left unchanged.",
        ok: true,
      });
    } catch (e) {
      setRmsg({ text: (e as Error).message, ok: false });
    }
  };
  const reset = async () => {
    setRmsg({ text: "resetting…", ok: true });
    try {
      await api.resetVoiceprint(tx.speaker_id!);
      setRmsg({ text: "Voiceprint cleared.", ok: true });
    } catch (e) {
      setRmsg({ text: (e as Error).message, ok: false });
    }
  };
  const assign = async () => {
    try {
      await api.assignSpeaker(tx.id, { label: name.trim() });
      closeModal();
    } catch (e) {
      setErr((e as Error).message);
    }
  };
  const rename = async () => {
    try {
      await api.renameSpeaker(tx.speaker_id!, name.trim());
      closeModal();
    } catch (e) {
      setErr((e as Error).message);
    }
  };

  return (
    <SqModal
      title="Who is this?"
      onClose={closeModal}
      footer={
        <>
          <button className="text-btn" onClick={closeModal}>
            Cancel
          </button>
          {tx.speaker_id != null && (
            <button className="text-btn" onClick={rename}>
              Rename voice
            </button>
          )}
          <button className="text-btn primary" onClick={assign}>
            Assign this TX
          </button>
        </>
      }
    >
      <p className="muted">
        {current
          ? `Currently: ${current}. Rename the voice everywhere, or assign just this transmission to someone else.`
          : "No voice match for this transmission. Assign it to a name to enroll this voice."}
      </p>
      <input
        type="text"
        placeholder="Name or callsign (e.g. W9ML Michael)"
        value={name}
        autoFocus
        onChange={(e) => setName(e.target.value)}
      />
      <div className="err">{err}</div>

      {tx.speaker_id != null && (
        <>
          <div className="vp-sep" />
          <p className="muted">
            Voiceprint out of whack? Rebuild keeps this speaker&apos;s dominant voice and drops
            outliers wrongly folded in. Reset wipes it entirely to re-learn from scratch.
          </p>
          <div className="vp-btns">
            <button className="text-btn" onClick={rebuild}>
              Rebuild voiceprint
            </button>
            {confirmReset ? (
              <span className="inline-confirm">
                Wipe it? Re-learns from new traffic.
                <button
                  className="link-danger"
                  onClick={() => {
                    setConfirmReset(false);
                    reset();
                  }}
                >
                  yes
                </button>
                <button className="link-muted" onClick={() => setConfirmReset(false)}>
                  no
                </button>
              </span>
            ) : (
              <button className="text-btn danger" onClick={() => setConfirmReset(true)}>
                Reset voiceprint
              </button>
            )}
          </div>
          <div className={rmsg.ok ? "ok-msg" : "err"}>{rmsg.text}</div>
        </>
      )}
    </SqModal>
  );
}
