"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import type { CallsignInfo } from "@/lib/types";
import { useApp } from "@/state/app-context";
import { SqModal } from "./SqModal";

function CallsignCard({
  d,
  onFind,
  onVoice,
}: {
  d: CallsignInfo;
  onFind: (cs: string) => void;
  onVoice: (id: number) => void;
}) {
  const facts: [string, string][] = [];
  const add = (k: string, v?: string) => {
    if (v) facts.push([k, v]);
  };
  if (d.status === "found") {
    add("Class", d.opclass ? d.opclass + (d.type === "club" ? " (club)" : "") : "");
    add("QTH", [d.city, d.state].filter(Boolean).join(", "));
    add("Grid", d.grid);
  }
  const none =
    (
      {
        not_found: "No US FCC record — may be a DX or special-event call.",
        disabled: "Callsign lookup is turned off.",
        error: "Lookup service unavailable — try again shortly.",
      } as Record<string, string>
    )[d.status] || "No license data.";

  return (
    <div className="cs-card">
      {d.status === "found" ? (
        <div className="cs-top">
          <div>
            {d.name && <div className="cs-name">{d.name}</div>}
            <div className="cs-facts">
              {facts.map(([k, v]) => (
                <span key={k} style={{ display: "contents" }}>
                  <span className="cs-k">{k}</span>
                  <span className="cs-v">{v}</span>
                </span>
              ))}
              {d.email && (
                <span style={{ display: "contents" }}>
                  <span className="cs-k">Email</span>
                  <span className="cs-v">
                    <a href={`mailto:${d.email}`}>{d.email}</a>
                  </span>
                </span>
              )}
            </div>
          </div>
          {d.image && (
            // QRZ profile photo (present with an XML subscription)
            // eslint-disable-next-line @next/next/no-img-element
            <img
              className="cs-photo"
              src={d.image}
              alt={`${d.callsign} QRZ photo`}
              onError={(e) => {
                (e.currentTarget as HTMLImageElement).style.display = "none";
              }}
            />
          )}
        </div>
      ) : (
        <div className="cs-none">{none}</div>
      )}
      <div className="cs-actions">
        <button className="text-btn" onClick={() => onFind(d.callsign)}>
          Find in feed
        </button>
        {d.speaker_id != null && (
          <button className="text-btn" onClick={() => onVoice(d.speaker_id!)}>
            View this voice
          </button>
        )}
        <a
          className="cs-qrz"
          href={`https://www.qrz.com/db/${encodeURIComponent(d.callsign)}`}
          target="_blank"
          rel="noopener noreferrer"
        >
          QRZ ↗
        </a>
      </div>
    </div>
  );
}

export function CallsignModal({ cs }: { cs: string }) {
  const { closeModal, setFilter, setSearch } = useApp();
  const [data, setData] = useState<CallsignInfo | null>(null);
  const [msg, setMsg] = useState("looking up license…");

  useEffect(() => {
    api
      .callsign(cs)
      .then(setData)
      .catch((e) => setMsg((e as Error).message));
  }, [cs]);

  return (
    <SqModal
      title={cs}
      onClose={closeModal}
      footer={
        <button className="text-btn primary" onClick={closeModal}>
          Close
        </button>
      }
    >
      {!data ? (
        <div className="cs-loading">{msg}</div>
      ) : (
        <CallsignCard
          d={data}
          onFind={(t) => {
            closeModal();
            setSearch(t);
          }}
          onVoice={(id) => {
            closeModal();
            setFilter("speaker_id", id);
          }}
        />
      )}
    </SqModal>
  );
}
