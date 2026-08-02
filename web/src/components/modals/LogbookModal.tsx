"use client";

import { useEffect, useMemo, useState } from "react";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { api } from "@/lib/api";
import { fmtAgo } from "@/lib/format";
import type { LogbookRow } from "@/lib/types";
import { useApp } from "@/state/app-context";
import { ICONS } from "../icons";
import { SqModal } from "./SqModal";

type Sort = "recent" | "count" | "callsign";

export function LogbookModal() {
  const { closeModal, setFilter, setSearch, openModal } = useApp();
  const [sort, setSort] = useState<Sort>("recent");

  // pull up an operator's transmissions: their whole voice history when the
  // callsign is linked to a speaker, otherwise a transcript search for the call
  const viewTransmissions = (c: LogbookRow) => {
    closeModal();
    if (c.speaker_id != null) setFilter("speaker_id", c.speaker_id);
    else setSearch(c.callsign);
  };
  const [rows, setRows] = useState<LogbookRow[] | null>(null);
  const [msg, setMsg] = useState("building the logbook…");

  useEffect(() => {
    api
      .callsigns()
      .then(({ callsigns }) => setRows(callsigns))
      .catch((e) => setMsg((e as Error).message));
  }, []);

  const sorted = useMemo(() => {
    if (!rows) return null;
    const r = [...rows];
    if (sort === "count") r.sort((a, b) => b.count - a.count);
    else if (sort === "callsign") r.sort((a, b) => a.callsign.localeCompare(b.callsign));
    // 'recent' keeps the server's last-heard-desc order
    return r;
  }, [rows, sort]);

  return (
    <SqModal
      title="Callsign logbook"
      wide
      onClose={closeModal}
      footer={
        <button className="text-btn primary" onClick={closeModal}>
          Close
        </button>
      }
    >
      <div className="seg">
        {(
          [
            ["recent", "Recent"],
            ["count", "Most heard"],
            ["callsign", "A–Z"],
          ] as [Sort, string][]
        ).map(([k, l]) => (
          <button key={k} className={"seg-btn" + (k === sort ? " on" : "")} onClick={() => setSort(k)}>
            {l}
          </button>
        ))}
      </div>

      <div className="logbook">
        {!sorted ? (
          <div className="dash-loading">{msg}</div>
        ) : sorted.length === 0 ? (
          <div className="dash-empty">No callsigns heard yet.</div>
        ) : (
          sorted.map((c) => {
            const meta = [[c.city, c.state].filter(Boolean).join(", "), c.opclass].filter(Boolean).join(" · ");
            return (
              <div
                className="lg-row lg-clickable"
                key={c.callsign}
                title={`Show ${c.callsign}'s transmissions`}
                onClick={() => viewTransmissions(c)}
              >
                <div className="lg-id">
                  <button
                    className="cs-chip"
                    title="Look up license (QRZ / FCC)"
                    onClick={(e) => {
                      e.stopPropagation();
                      openModal({ kind: "callsign", cs: c.callsign });
                    }}
                  >
                    {c.callsign}
                  </button>
                </div>
                <div className="lg-who">
                  {c.name && <span className="lg-name">{c.name}</span>}
                  {meta ? (
                    <span className="lg-meta">{meta}</span>
                  ) : (
                    !c.name && (
                      <span className="lg-meta muted">
                        {c.status === "not_found"
                          ? "no US FCC record (DX or garbled?)"
                          : "not looked up yet"}
                      </span>
                    )
                  )}
                </div>
                <div className="lg-stats">
                  <span className="lg-count">{`${c.count}×`}</span>
                  <span className="lg-last">{fmtAgo(c.last_heard)}</span>
                </div>
                <span className="lg-voice" aria-hidden="true" title="Show transmissions">
                  <FontAwesomeIcon icon={c.speaker_id != null ? ICONS.similar : ICONS.search} />
                </span>
              </div>
            );
          })
        )}
      </div>
    </SqModal>
  );
}
