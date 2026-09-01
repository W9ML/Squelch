"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { datetimeToEpoch, epochToDatetimeInput } from "@/lib/format";
import type { Facets, SpeakerFacet } from "@/lib/types";
import { useApp } from "@/state/app-context";
import { SqModal } from "./SqModal";

export function FiltersModal() {
  const { filters: f, applyFilters, clearAllFilters, closeModal, status, speakers } = useApp();
  const [facets, setFacets] = useState<Facets | null>(null);
  const [from, setFrom] = useState(f.since ? epochToDatetimeInput(Number(f.since)) : "");
  const [to, setTo] = useState(f.until ? epochToDatetimeInput(Number(f.until)) : "");
  const [origin, setOrigin] = useState(f.origin != null ? String(f.origin) : "");
  const [speakerId, setSpeakerId] = useState(f.speaker_id != null ? String(f.speaker_id) : "");
  const [mdcUnit, setMdcUnit] = useState(f.mdc_unit != null ? String(f.mdc_unit) : "");
  const [hasMdc, setHasMdc] = useState(!!f.has_mdc);
  const [unnamed, setUnnamed] = useState(!!f.unnamed);
  const [noSpeech, setNoSpeech] = useState(!!f.no_speech);

  useEffect(() => {
    api.facets().then(setFacets).catch(() => {});
  }, []);
  useEffect(() => {
    if (facets) for (const s of facets.speakers) speakers.set(s.id, s.label);
  }, [facets, speakers]);

  // named operators alphabetized (that's the callsign filter people use),
  // then the unnamed "Speaker #" clusters after them
  const byLabel = (a: SpeakerFacet, b: SpeakerFacet) => a.label.localeCompare(b.label);
  const named = facets ? facets.speakers.filter((s) => s.is_named).sort(byLabel) : [];
  const rest = facets ? facets.speakers.filter((s) => !s.is_named).sort(byLabel) : [];

  const apply = () => {
    applyFilters({
      since: from ? datetimeToEpoch(from) : null,
      until: to ? datetimeToEpoch(to) : null,
      origin: origin || null,
      speaker_id: speakerId || null,
      mdc_unit: mdcUnit || null,
      has_mdc: hasMdc || null,
      unnamed: unnamed || null,
      no_speech: noSpeech || null,
    });
    closeModal();
  };

  return (
    <SqModal
      title="Filter transmissions"
      onClose={closeModal}
      footer={
        <>
          <button
            className="text-btn"
            onClick={() => {
              closeModal();
              clearAllFilters();
            }}
          >
            Clear all
          </button>
          <button className="text-btn primary" onClick={apply}>
            Apply
          </button>
        </>
      }
    >
      <label>Date &amp; time range</label>
      <div className="filter-row">
        <div>
          <label>From</label>
          <input type="datetime-local" value={from} onChange={(e) => setFrom(e.target.value)} />
        </div>
        <div>
          <label>To</label>
          <input type="datetime-local" value={to} onChange={(e) => setTo(e.target.value)} />
        </div>
      </div>

      <label>Source / node</label>
      <select value={origin} onChange={(e) => setOrigin(e.target.value)}>
        <option value="">Any source</option>
        {facets?.origins.map((o) => (
          <option key={o} value={o}>
            {o === "local" ? `${status?.callsign || "RF"} · local` : `node ${o}`}
          </option>
        ))}
      </select>

      <label>Operator (callsign)</label>
      <select value={speakerId} onChange={(e) => setSpeakerId(e.target.value)}>
        <option value="">Any operator</option>
        {[...named, ...rest].map((s) => (
          <option key={s.id} value={String(s.id)}>
            {s.label}
          </option>
        ))}
      </select>

      <label>MDC unit ID</label>
      <select value={mdcUnit} onChange={(e) => setMdcUnit(e.target.value)}>
        <option value="">Any MDC unit</option>
        {facets?.mdc_units.map((u) => (
          <option key={u} value={u}>
            {u}
          </option>
        ))}
      </select>

      <label className="filter-check">
        <input type="checkbox" checked={hasMdc} onChange={(e) => setHasMdc(e.target.checked)} /> Only
        transmissions with MDC data
      </label>
      <label className="filter-check">
        <input type="checkbox" checked={unnamed} onChange={(e) => setUnnamed(e.target.checked)} /> Only
        unidentified (Speaker #) transmissions
      </label>
      <label className="filter-check">
        <input type="checkbox" checked={noSpeech} onChange={(e) => setNoSpeech(e.target.checked)} /> Only
        non-speech transmissions (audio, no words — jamming / interference)
      </label>
    </SqModal>
  );
}
