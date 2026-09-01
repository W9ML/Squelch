"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { api } from "@/lib/api";
import type { CaseDetail, CaseSummary, CasesResponse } from "@/lib/types";
import { useApp } from "@/state/app-context";
import { ICONS } from "../icons";
import { SqModal } from "./SqModal";

type CasePatch = Partial<{
  title: string; status: string; subject: string; summary: string;
}>;

const STATUS_LABEL: Record<string, string> = {
  open: "Open", active: "Active", suspended: "Suspended",
  closed: "Closed", referred: "Referred",
};

function fmtWhen(epoch: number): string {
  return new Date(epoch * 1000).toLocaleString([], {
    year: "numeric", month: "short", day: "numeric",
    hour: "numeric", minute: "2-digit",
  });
}
function fmtSecs(ms: number | null): string {
  const s = Math.round((ms || 0) / 1000);
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${s % 60}s`;
}

// ---------- new-case form ----------
function NewCase({ onCreate, onCancel }: {
  onCreate: (c: CaseDetail) => void;
  onCancel: () => void;
}) {
  const [title, setTitle] = useState("");
  const [subject, setSubject] = useState("");
  const [summary, setSummary] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    if (!title.trim()) return setErr("give the case a title");
    setBusy(true); setErr("");
    try {
      const { case: c } = await api.createCase({
        title: title.trim(), subject: subject.trim(), summary: summary.trim(),
      });
      onCreate(c);
    } catch (e) { setErr((e as Error).message); setBusy(false); }
  };

  return (
    <div className="case-form">
      <label htmlFor="nc-title">Title</label>
      <input id="nc-title" autoFocus value={title}
        placeholder="e.g. Malicious carrier during Tuesday net"
        onChange={(e) => setTitle(e.target.value)} />
      <label htmlFor="nc-subject">Suspected operator <span className="hint">(callsign or description, optional)</span></label>
      <input id="nc-subject" value={subject} onChange={(e) => setSubject(e.target.value)} />
      <label htmlFor="nc-summary">Summary</label>
      <textarea id="nc-summary" rows={3} value={summary} onChange={(e) => setSummary(e.target.value)} />
      {err ? <div className="err" role="alert">{err}</div> : null}
      <div className="modal-actions">
        <button className="text-btn" onClick={onCancel}>Cancel</button>
        <button className="text-btn primary" disabled={busy} onClick={submit}>
          {busy ? "Opening…" : "Open case"}
        </button>
      </div>
    </div>
  );
}

// ---------- case detail ----------
function Detail({ caseId, statuses, isSuper, onBack }: {
  caseId: number; statuses: string[]; isSuper: boolean;
  onBack: () => void;
}) {
  const [c, setC] = useState<CaseDetail | null>(null);
  const [note, setNote] = useState("");
  const [msg, setMsg] = useState("loading…");
  // themed confirm dialogs, in place of native window.prompt / confirm
  const [dialog, setDialog] = useState<
    | { kind: "closure"; next: string; verb: string }
    | { kind: "delete" }
    | null
  >(null);
  const [dlgNote, setDlgNote] = useState("");
  const [dlgBusy, setDlgBusy] = useState(false);
  const [err, setErr] = useState("");        // inline detail error (themed, not alert)
  const [dlgErr, setDlgErr] = useState("");  // inline error inside the open dialog
  const [noteBusy, setNoteBusy] = useState(false);

  const load = useCallback(() => {
    api.case(caseId).then(setC).catch((e) => setMsg((e as Error).message));
  }, [caseId]);
  useEffect(load, [load]);

  // optimistic: reflect the change instantly, then reconcile with the server
  const patch = async (body: CasePatch) => {
    setErr("");
    setC((cur) => (cur ? { ...cur, ...body } : cur));
    try { const { case: nc } = await api.updateCase(caseId, body); setC(nc); }
    catch (e) { setErr((e as Error).message); load(); }
  };
  const openDialog = (d: Exclude<typeof dialog, null>) => { setDlgErr(""); setDialog(d); };
  const changeStatus = (next: string) => {
    if (!c || next === c.status) return;
    // hold Closed/Referred behind a themed confirm + closure note
    if (next === "closed" || next === "referred") {
      setDlgNote("");
      openDialog({ kind: "closure", next, verb: next === "closed" ? "Close" : "Refer" });
      return;
    }
    patch({ status: next });
  };
  const confirmClosure = async () => {
    if (dialog?.kind !== "closure" || !c) return;
    const { next, verb } = dialog;
    setDlgBusy(true); setDlgErr("");
    setC((cur) => (cur ? { ...cur, status: next } : cur));   // optimistic
    try {
      await api.updateCase(caseId, { status: next });
      if (dlgNote.trim()) await api.addCaseNote(caseId, `[${verb}d] ${dlgNote.trim()}`);
      setC(await api.case(caseId));            // reconcile: log entries + closed_at
      setDialog(null);
    } catch (e) { setDlgErr((e as Error).message); load(); }   // keep dialog open to retry
    finally { setDlgBusy(false); }
  };
  const removeItem = async (itemId: number) => {
    setErr("");
    try { const { case: nc } = await api.removeCaseItem(caseId, itemId); setC(nc); }
    catch (e) { setErr((e as Error).message); }
  };
  const addNote = async () => {
    const t = note.trim(); if (!t || noteBusy) return;   // guard double-submit
    setErr(""); setNoteBusy(true);
    try { const { case: nc } = await api.addCaseNote(caseId, t); setC(nc); setNote(""); }
    catch (e) { setErr((e as Error).message); }   // keep the typed note on failure
    finally { setNoteBusy(false); }
  };
  const confirmDelete = async () => {
    setDlgBusy(true); setDlgErr("");
    try { await api.deleteCase(caseId); onBack(); }
    catch (e) { setDlgErr((e as Error).message); setDlgBusy(false); }  // keep dialog open
  };

  if (!c) return <div className="dash-loading">{msg}</div>;

  return (
    <>
    <div className="case-detail">
      <div className="case-detail-top">
        <button className="text-btn" onClick={onBack}>← All cases</button>
        <span className={"case-status s-" + c.status}>{STATUS_LABEL[c.status] || c.status}</span>
      </div>
      {err ? <div className="err case-err" role="alert">{err}</div> : null}

      <div className="case-head">
        <span className="case-num">Case {c.number}</span>
        <input className="case-title-edit" aria-label="Case title" defaultValue={c.title}
          onBlur={(e) => { const v = e.target.value.trim(); if (v && v !== c.title) patch({ title: v }); }} />
      </div>

      <div className="case-meta">
        <label htmlFor="cd-status">Status</label>
        <select id="cd-status" className="native-select" value={c.status} onChange={(e) => changeStatus(e.target.value)}>
          {statuses.map((s) => <option key={s} value={s}>{STATUS_LABEL[s] || s}</option>)}
        </select>
        <label htmlFor="cd-subject">Suspected operator</label>
        <input id="cd-subject" defaultValue={c.subject || ""} placeholder="callsign or description"
          onBlur={(e) => { if (e.target.value !== (c.subject || "")) patch({ subject: e.target.value }); }} />
      </div>

      <label className="case-lbl" htmlFor="cd-summary">Summary</label>
      <textarea id="cd-summary" className="case-summary" rows={2} defaultValue={c.summary || ""}
        placeholder="what happened…"
        onBlur={(e) => { if (e.target.value !== (c.summary || "")) patch({ summary: e.target.value }); }} />

      <h4>Evidence — {c.items.length} recording{c.items.length === 1 ? "" : "s"}</h4>
      {c.items.length === 0 ? (
        <div className="dash-empty">No recordings yet. Add one from the ⋯ menu on any transmission.</div>
      ) : (
        <div className="case-ev">
          {c.items.map((it) => (
            <div className="case-ev-row" key={it.id}>
              <div className="case-ev-meta">
                <span className="case-ev-when">{fmtWhen(it.started_at)}</span>
                <span className="case-ev-sub">
                  {fmtSecs(it.duration_ms)} · tx {it.tx_id}
                  {it.origin ? ` · from ${it.origin}` : ""}
                  {it.origin_hub ? ` · hub ${it.origin_hub}` : ""}
                </span>
                {it.label ? <span className="case-ev-label">{it.label}</span> : null}
                {it.note ? <span className="case-ev-note">{it.note}</span> : null}
              </div>
              {it.has_audio ? (
                // eslint-disable-next-line jsx-a11y/media-has-caption
                <audio className="case-ev-audio" controls preload="none" src={api.audioUrl(it.tx_id)} />
              ) : (
                <span className="case-ev-gone">audio purged</span>
              )}
              <button className="case-ev-x" aria-label="Remove from case" title="Remove from case" onClick={() => removeItem(it.id)}>×</button>
            </div>
          ))}
        </div>
      )}

      <h4>Activity log</h4>
      <div className="case-log">
        {c.notes.map((n) => (
          <div className={"case-log-row" + (n.kind === "system" ? " sys" : "")} key={n.id}>
            <span className="case-log-ts">{fmtWhen(n.ts)}</span>
            <span className="case-log-au">{n.author || "system"}</span>
            <span className="case-log-tx">{n.text}</span>
          </div>
        ))}
      </div>
      <div className="case-addnote">
        <input value={note} aria-label="Add a note to the activity log"
          placeholder="Add a note to the log…" disabled={noteBusy}
          onChange={(e) => setNote(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") addNote(); }} />
        <button className="text-btn" disabled={noteBusy} onClick={addNote}>
          {noteBusy ? "Adding…" : "Add note"}
        </button>
      </div>

      <div className="case-detail-foot">
        <a className="text-btn" href={api.caseExportUrl(caseId)} target="_blank" rel="noopener">
          Export report
        </a>
        <a className="text-btn" href={api.caseExportZipUrl(caseId)}>
          Export + audio (ZIP)
        </a>
        {isSuper ? <button className="text-btn danger" onClick={() => openDialog({ kind: "delete" })}>Delete case</button> : null}
      </div>
    </div>

    {dialog?.kind === "closure" && (
      <SqModal
        title={`${dialog.verb} case ${c.number}`}
        onClose={() => { if (!dlgBusy) setDialog(null); }}
        footer={<>
          <button className="text-btn" disabled={dlgBusy} onClick={() => setDialog(null)}>Cancel</button>
          <button className="text-btn primary" disabled={dlgBusy} onClick={confirmClosure}>
            {dlgBusy ? "Saving…" : `${dialog.verb} case`}
          </button>
        </>}
      >
        <div className="case-form">
          <label>Closure note <span className="hint">(outcome / disposition — filed in the activity log)</span></label>
          <textarea autoFocus rows={4} value={dlgNote}
            placeholder="e.g. Referred to the frequency coordinator; operator identified via voiceprint."
            onChange={(e) => setDlgNote(e.target.value)} />
          {dlgErr ? <div className="err" role="alert">{dlgErr}</div> : null}
        </div>
      </SqModal>
    )}

    {dialog?.kind === "delete" && (
      <SqModal
        title={`Delete case ${c.number}?`}
        onClose={() => { if (!dlgBusy) setDialog(null); }}
        footer={<>
          <button className="text-btn" disabled={dlgBusy} onClick={() => setDialog(null)}>Cancel</button>
          <button className="text-btn danger" disabled={dlgBusy} onClick={confirmDelete}>
            {dlgBusy ? "Deleting…" : "Delete case"}
          </button>
        </>}
      >
        <p className="case-dlg-lead">
          Its evidence links and activity log are removed. The recordings themselves stay in the archive.
        </p>
        {dlgErr ? <div className="err" role="alert">{dlgErr}</div> : null}
      </SqModal>
    )}
    </>
  );
}

// ---------- top-level modal ----------
export function CasesModal() {
  const { closeModal, status } = useApp();
  const isSuper = !!status?.is_super;
  const [data, setData] = useState<CasesResponse | null>(null);
  const [msg, setMsg] = useState("loading cases…");
  const [filter, setFilter] = useState("all");
  const [sel, setSel] = useState<number | null>(null);
  const [creating, setCreating] = useState(false);

  const reload = useCallback(() => {
    api.cases().then((d) => { setData(d); if (!d.cases.length) setMsg("No cases yet."); })
      .catch((e) => setMsg((e as Error).message));
  }, []);
  useEffect(reload, [reload]);

  const shown = useMemo(() => {
    if (!data) return [] as CaseSummary[];
    return filter === "all" ? data.cases : data.cases.filter((c) => c.status === filter);
  }, [data, filter]);

  if (sel != null && data) {
    return (
      <SqModal title="Cases" wide onClose={closeModal}
        footer={<button className="text-btn primary" onClick={closeModal}>Close</button>}>
        <Detail caseId={sel} statuses={data.statuses}
          isSuper={isSuper} onBack={() => { reload(); setSel(null); }} />
      </SqModal>
    );
  }

  return (
    <SqModal title="Cases" wide onClose={closeModal}
      footer={<button className="text-btn primary" onClick={closeModal}>Close</button>}>
      {creating && data ? (
        <NewCase
          onCreate={(c) => { setCreating(false); reload(); setSel(c.id); }}
          onCancel={() => setCreating(false)} />
      ) : (
        <>
          <div className="case-toolbar">
            <div className="seg">
              {["all", ...(data?.statuses || [])].map((s) => (
                <button key={s} className={"seg-btn" + (filter === s ? " on" : "")}
                  onClick={() => setFilter(s)}>
                  {s === "all" ? "All" : (STATUS_LABEL[s] || s)}
                </button>
              ))}
            </div>
            <button className="text-btn primary case-new" onClick={() => setCreating(true)}>
              <FontAwesomeIcon icon={ICONS.addCase} /> New case
            </button>
          </div>

          {!data ? (
            <div className="dash-loading">{msg}</div>
          ) : shown.length === 0 ? (
            <div className="dash-empty">{data.cases.length ? "No cases with that status." : msg}</div>
          ) : (
            <div className="case-table-wrap">
              <table className="case-table">
                <thead>
                  <tr><th>Case</th><th>Title</th><th>Status</th><th>Evidence</th><th>Opened</th></tr>
                </thead>
                <tbody>
                  {shown.map((c) => (
                    <tr key={c.id} tabIndex={0} role="button"
                      aria-label={`Open case ${c.number}: ${c.title}`}
                      onClick={() => setSel(c.id)}
                      onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); setSel(c.id); } }}>
                      <td className="case-tnum">{c.number}</td>
                      <td>{c.title}{c.subject ? <span className="case-tsub"> · {c.subject}</span> : null}</td>
                      <td><span className={"case-status s-" + c.status}>{STATUS_LABEL[c.status] || c.status}</span></td>
                      <td>{c.item_count}</td>
                      <td className="muted">{fmtWhen(c.opened_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </SqModal>
  );
}
