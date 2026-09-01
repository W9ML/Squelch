"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import type { CaseSummary, Transmission } from "@/lib/types";
import { useApp } from "@/state/app-context";
import { SqModal } from "./SqModal";

/** File a single transmission as evidence under a case (or open a new one).
 *  Reached from the ⋯ menu on a transmission card; super/admin only. */
export function AddToCaseModal({ tx }: { tx: Transmission }) {
  const { closeModal } = useApp();
  const [cases, setCases] = useState<CaseSummary[] | null>(null);
  const [sel, setSel] = useState<number | "new">("new");
  const [newTitle, setNewTitle] = useState("");
  const [label, setLabel] = useState("");
  const [note, setNote] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState("");

  useEffect(() => {
    api.cases().then((d) => {
      setCases(d.cases);
      setSel(d.cases.length ? d.cases[0].id : "new");
    }).catch((e) => setErr((e as Error).message));
  }, []);

  const submit = async () => {
    setErr(""); setBusy(true);
    try {
      let caseId: number;
      let number = "";
      if (sel === "new") {
        if (!newTitle.trim()) { setErr("name the new case"); setBusy(false); return; }
        const { case: c } = await api.createCase({ title: newTitle.trim() });
        caseId = c.id; number = c.number;
      } else {
        caseId = sel;
        number = cases?.find((c) => c.id === sel)?.number || "";
      }
      const r = await api.addCaseItem(caseId, tx.id, label.trim(), note.trim());
      setDone(r.already ? `Already filed under case ${number}.` : `Added to case ${number}.`);
      setBusy(false);
    } catch (e) { setErr((e as Error).message); setBusy(false); }
  };

  return (
    <SqModal
      title="Add recording to a case"
      onClose={closeModal}
      footer={
        done ? (
          <button className="text-btn primary" onClick={closeModal}>Done</button>
        ) : (
          <>
            <button className="text-btn" onClick={closeModal}>Cancel</button>
            <button className="text-btn primary" disabled={busy || !cases} onClick={submit}>
              {busy ? "Filing…" : "Add to case"}
            </button>
          </>
        )
      }
    >
      <div className="atc-tx">
        tx {tx.id} · {new Date(tx.started_at * 1000).toLocaleString()} ·{" "}
        {Math.round((tx.duration_ms || 0) / 1000)}s
        {tx.origin ? ` · from ${tx.origin}` : ""}
      </div>

      {done ? (
        <div className="atc-done">{done}</div>
      ) : (
        <>
          <label>Case</label>
          <select className="native-select atc-sel" value={String(sel)}
            onChange={(e) => setSel(e.target.value === "new" ? "new" : Number(e.target.value))}>
            {(cases || []).map((c) => (
              <option key={c.id} value={c.id}>
                {c.number} — {c.title} ({c.status})
              </option>
            ))}
            <option value="new">＋ New case…</option>
          </select>
          {sel === "new" ? (
            <input autoFocus placeholder="New case title" value={newTitle}
              onChange={(e) => setNewTitle(e.target.value)} />
          ) : null}
          <label>Label <span className="hint">(optional)</span></label>
          <input value={label} placeholder="e.g. explicit audio"
            onChange={(e) => setLabel(e.target.value)} />
          <label>Note <span className="hint">(optional)</span></label>
          <input value={note} placeholder="why this matters"
            onChange={(e) => setNote(e.target.value)} />
          <div className="err">{err}</div>
        </>
      )}
    </SqModal>
  );
}
