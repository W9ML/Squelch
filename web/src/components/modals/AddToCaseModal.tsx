"use client";

import { useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import type { CaseSummary, Transmission } from "@/lib/types";
import { useApp } from "@/state/app-context";
import { SqModal } from "./SqModal";

/** File one or more transmissions as evidence under a case (or open a new one).
 *  Single-recording: the ⋯ menu on a card. Many: the feed's multi-select bar.
 *  Super/admin only. */
export function AddToCaseModal({ txs, onFiled }: {
  txs: Transmission[];
  onFiled?: () => void;   // called once the add succeeds (feed uses it to clear the selection)
}) {
  const { closeModal } = useApp();
  const single = txs.length === 1 ? txs[0] : null;
  const [cases, setCases] = useState<CaseSummary[] | null>(null);
  const [sel, setSel] = useState<number | "new">("new");
  const [newTitle, setNewTitle] = useState("");
  const [label, setLabel] = useState("");
  const [note, setNote] = useState("");
  const [filed, setFiled] = useState<{ number: string }[]>([]);   // cases it's already in
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState("");
  // a case created in THIS session — reused on retry so a failed file-step
  // can't leave a trail of empty duplicate cases
  const createdRef = useRef<{ id: number; number: string } | null>(null);

  useEffect(() => {
    api.cases().then((d) => {
      setCases(d.cases);
      setSel(d.cases.length ? d.cases[0].id : "new");
    }).catch((e) => { setCases([]); setErr((e as Error).message); });   // still allow "new"
    if (single) api.txCases(single.id).then((d) => setFiled(d.cases)).catch(() => {});
  }, [single]);

  const submit = async () => {
    setErr(""); setBusy(true);
    try {
      let caseId: number;
      let number = "";
      if (sel === "new") {
        if (!newTitle.trim()) { setErr("name the new case"); setBusy(false); return; }
        if (createdRef.current) {                       // reuse, don't re-create
          ({ id: caseId, number } = createdRef.current);
        } else {
          const { case: c } = await api.createCase({ title: newTitle.trim() });
          createdRef.current = { id: c.id, number: c.number };
          caseId = c.id; number = c.number;
        }
      } else {
        caseId = sel;
        number = cases?.find((c) => c.id === sel)?.number || "";
      }
      if (single) {
        const r = await api.addCaseItem(caseId, single.id, label.trim(), note.trim());
        setDone(r.already ? `Already filed under case ${number}.` : `Added to case ${number}.`);
      } else {
        const r = await api.addCaseItemsBulk(
          caseId, txs.map((t) => t.id), label.trim(), note.trim());
        const bits = [`Added ${r.added} to case ${number}`];
        if (r.already) bits.push(`${r.already} already filed`);
        if (r.missing) bits.push(`${r.missing} no longer available`);
        setDone(bits.join(" · ") + ".");
      }
      onFiled?.();   // success — let the feed clear its selection
    } catch (e) { setErr((e as Error).message); }
    finally { setBusy(false); }
  };

  const verb = single ? "Add to case" : `Add ${txs.length} to case`;

  return (
    <SqModal
      title={single ? "Add recording to a case" : `Add ${txs.length} recordings to a case`}
      onClose={closeModal}
      footer={
        done ? (
          <button className="text-btn primary" onClick={closeModal}>Done</button>
        ) : (
          <>
            <button className="text-btn" onClick={closeModal}>Cancel</button>
            <button className="text-btn primary" disabled={busy || cases === null} onClick={submit}>
              {busy ? "Filing…" : verb}
            </button>
          </>
        )
      }
    >
      {single ? (
        <div className="atc-tx">
          tx {single.id} · {new Date(single.started_at * 1000).toLocaleString()} ·{" "}
          {Math.round((single.duration_ms || 0) / 1000)}s
          {single.origin ? ` · from ${single.origin}` : ""}
        </div>
      ) : (
        <div className="atc-tx">{txs.length} recordings selected</div>
      )}

      {filed.length ? (
        <div className="atc-filed">
          Already filed in: {filed.map((c) => c.number).join(", ")}
        </div>
      ) : null}

      {done ? (
        <div className="atc-done">{done}</div>
      ) : (
        <>
          <label htmlFor="atc-case">Case</label>
          <select id="atc-case" className="native-select atc-sel" value={String(sel)}
            onChange={(e) => setSel(e.target.value === "new" ? "new" : Number(e.target.value))}>
            {(cases || []).map((c) => (
              <option key={c.id} value={c.id}>
                {c.number} — {c.title} ({c.status})
              </option>
            ))}
            <option value="new">＋ New case…</option>
          </select>
          {sel === "new" ? (
            <input id="atc-newtitle" autoFocus aria-label="New case title"
              placeholder="New case title" value={newTitle}
              onChange={(e) => setNewTitle(e.target.value)} />
          ) : null}
          <label htmlFor="atc-label">Label <span className="hint">(optional)</span></label>
          <input id="atc-label" value={label} placeholder="e.g. explicit audio"
            onChange={(e) => setLabel(e.target.value)} />
          <label htmlFor="atc-note">Note <span className="hint">(optional)</span></label>
          <input id="atc-note" value={note} placeholder="why this matters"
            onChange={(e) => setNote(e.target.value)} />
          {err ? <div className="err" role="alert">{err}</div> : null}
        </>
      )}
    </SqModal>
  );
}
