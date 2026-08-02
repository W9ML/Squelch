"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import type { SpeakerDetail } from "@/lib/types";
import { useApp } from "@/state/app-context";
import { Avatar } from "./Avatar";

function labelFor(
  key: string,
  v: string | number | boolean,
  speakers: Map<number, string>,
  callsign?: string,
): string {
  switch (key) {
    case "speaker_id":
      return `speaker: ${speakers.get(+v) || "#" + v}`;
    case "origin":
      return v === "local" ? `origin: ${callsign || "local"} repeater` : `origin: node ${v}`;
    case "mdc_unit":
      return `MDC unit ${v}`;
    case "since":
      return `from ${new Date(Number(v) * 1000).toLocaleDateString()}`;
    case "until":
      return `to ${new Date((Number(v) - 1) * 1000).toLocaleDateString()}`;
    case "has_mdc":
      return "with MDC data";
    case "unnamed":
      return "unidentified (Speaker #)";
    default:
      return `${key}: ${v}`;
  }
}

function SpeakerBanner({ speakerId }: { speakerId: number }) {
  const { isAdmin, openModal, speakers } = useApp();
  const [s, setS] = useState<SpeakerDetail | null>(null);

  useEffect(() => {
    let alive = true;
    api
      .speaker(speakerId)
      .then((d) => alive && setS(d))
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [speakerId]);

  if (!s) return null;
  const air = s.airtime_ms / 1000;
  const airStr = air > 60 ? `${Math.floor(air / 60)}m ${Math.round(air % 60)}s` : `${air.toFixed(0)}s`;
  const max = Math.max(1, ...s.hourly);

  return (
    <div className="speaker-banner">
      <Avatar label={s.label} />
      <div className="sb-info">
        <div className="sb-name">{s.label}</div>
        <div className="sb-stats">
          {`${s.tx_count} transmissions · ${airStr} on air`}
          {s.last_heard ? ` · last heard ${new Date(s.last_heard * 1000).toLocaleString()}` : ""}
        </div>
        <div className="sb-heat">
          {s.hourly.map((h, i) => (
            <i key={i} style={{ opacity: 0.15 + 0.85 * (h / max) }} title={`${h} at ${i}:00`} />
          ))}
        </div>
      </div>
      {isAdmin && (
        <button
          className="text-btn"
          onClick={() =>
            openModal({ kind: "renameSpeaker", speakerId, label: speakers.get(speakerId) || s.label })
          }
        >
          rename / merge
        </button>
      )}
    </div>
  );
}

export function FilterBar() {
  const { filters, searching, searchText, isFiltered, clearFilter, clearAllFilters, setSearch, speakers, status } =
    useApp();
  if (!isFiltered) return null;
  const keys = Object.keys(filters);

  return (
    <div id="filter-bar">
      {filters.speaker_id != null && <SpeakerBanner speakerId={Number(filters.speaker_id)} />}
      <div className="filter-chips">
        {searching && (
          <span className="filter-chip">
            {`“${searchText.trim()}”`}
            <button onClick={() => setSearch("")}>×</button>
          </span>
        )}
        {keys.map((k) => (
          <span className="filter-chip" key={k}>
            {labelFor(k, filters[k], speakers, status?.callsign)}
            <button onClick={() => clearFilter(k)}>×</button>
          </span>
        ))}
        <button className="text-btn filter-clear" onClick={clearAllFilters}>
          clear all
        </button>
      </div>
    </div>
  );
}
