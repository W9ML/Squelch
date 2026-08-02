"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { DOW, fmtAirtime, epochToDatetimeInput, datetimeToEpoch } from "@/lib/format";
import type { Stats } from "@/lib/types";
import { useApp } from "@/state/app-context";
import { SqModal } from "./SqModal";

function busiestHour(heat: number[][]): string {
  const byHour = new Array(24).fill(0);
  for (let d = 0; d < 7; d++) for (let h = 0; h < 24; h++) byHour[h] += heat[d][h];
  let mh = 0;
  for (let h = 1; h < 24; h++) if (byHour[h] > byHour[mh]) mh = h;
  return byHour[mh] ? `${String(mh).padStart(2, "0")}:00` : "—";
}

function Heatmap({ heat }: { heat: number[][] }) {
  let max = 0;
  for (let d = 0; d < 7; d++) for (let h = 0; h < 24; h++) if (heat[d][h] > max) max = heat[d][h];
  return (
    <div className="heatmap">
      <div className="heat-row heat-head">
        <span className="heat-daylabel" />
        {Array.from({ length: 24 }, (_, h) => (
          <span key={h} className="heat-hourlabel">
            {h % 6 === 0 ? String(h) : ""}
          </span>
        ))}
      </div>
      {Array.from({ length: 7 }, (_, d) => (
        <div className="heat-row" key={d}>
          <span className="heat-daylabel">{DOW[d]}</span>
          {Array.from({ length: 24 }, (_, h) => {
            const v = heat[d][h];
            const pct = max ? Math.round(14 + 86 * (v / max)) : 0;
            return (
              <span
                key={h}
                className="heat-cell"
                title={`${DOW[d]} ${String(h).padStart(2, "0")}:00 — ${v} tx`}
                style={v ? { background: `color-mix(in srgb, var(--accent) ${pct}%, transparent)` } : undefined}
              />
            );
          })}
        </div>
      ))}
    </div>
  );
}

// selectable time windows: [key, label, api params]
const RANGES: [string, string, { days: number; today?: boolean }][] = [
  ["today", "Today", { days: 1, today: true }],
  ["24h", "24 hrs", { days: 1 }],
  ["7", "7 days", { days: 7 }],
  ["30", "30 days", { days: 30 }],
  ["90", "90 days", { days: 90 }],
  ["all", "All", { days: 0 }],
];

// resolve node/hub numbers -> friendly callsign/alias, cached across renders
const nodeNameCache = new Map<string, Promise<string | null>>();
function nodeName(num: string): Promise<string | null> {
  if (!nodeNameCache.has(num)) {
    nodeNameCache.set(num, api.node(num).then((i) => i?.callsign || null).catch(() => null));
  }
  return nodeNameCache.get(num)!;
}

export function StatsModal() {
  const { closeModal, setFilter } = useApp();
  const [range, setRange] = useState("30");
  const [data, setData] = useState<Stats | null>(null);
  const [msg, setMsg] = useState("crunching the numbers…");
  const [from, setFrom] = useState(() => epochToDatetimeInput(Date.now() / 1000 - 3600));
  const [to, setTo] = useState(() => epochToDatetimeInput(Date.now() / 1000));
  const [names, setNames] = useState<Record<string, string>>({});

  useEffect(() => {
    setData(null);
    setMsg("crunching the numbers…");
    const tz = -new Date().getTimezoneOffset();
    if (range === "custom") {
      const since = from ? datetimeToEpoch(from) : null;
      const until = to ? datetimeToEpoch(to) : null;
      api.stats(0, tz, false, since, until).then(setData).catch((e) => setMsg((e as Error).message));
    } else {
      const params = RANGES.find(([k]) => k === range)?.[2] ?? { days: 30 };
      api.stats(params.days, tz, params.today).then(setData).catch((e) => setMsg((e as Error).message));
    }
  }, [range, from, to]);

  // resolve the node/hub numbers that show up in the breakdowns
  useEffect(() => {
    if (!data) return;
    const nums = new Set<string>();
    (data.by_node || []).forEach((x) => x.node && nums.add(x.node));
    (data.by_hub || []).forEach((x) => x.hub && nums.add(x.hub));
    nums.forEach((n) =>
      nodeName(n).then((nm) => {
        if (nm) setNames((p) => (p[n] ? p : { ...p, [n]: nm }));
      }),
    );
  }, [data]);

  const pick = (id: number) => {
    closeModal();
    setFilter("speaker_id", id);
  };

  return (
    <SqModal
      title="Activity"
      wide
      onClose={closeModal}
      footer={
        <button className="text-btn primary" onClick={closeModal}>
          Close
        </button>
      }
    >
      <div className="seg">
        {RANGES.map(([k, label]) => (
          <button
            key={k}
            className={"seg-btn" + (k === range ? " on" : "")}
            onClick={() => setRange(k)}
          >
            {label}
          </button>
        ))}
        <button
          className={"seg-btn" + (range === "custom" ? " on" : "")}
          onClick={() => setRange("custom")}
        >
          Net window
        </button>
      </div>
      {range === "custom" && (
        <div className="filter-row stats-window">
          <div>
            <label>From</label>
            <input type="datetime-local" value={from} onChange={(e) => setFrom(e.target.value)} />
          </div>
          <div>
            <label>To</label>
            <input type="datetime-local" value={to} onChange={(e) => setTo(e.target.value)} />
          </div>
        </div>
      )}

      <div className="dash">
        {!data ? (
          <div className="dash-loading">{msg}</div>
        ) : (
          <>
            <div className="dash-cards">
              {(
                [
                  [data.totals.count.toLocaleString(), "transmissions"],
                  [fmtAirtime(data.totals.airtime_ms), "total airtime"],
                  [String(data.totals.speakers), "active voices"],
                  [data.totals.kerchunks.toLocaleString(), "kerchunks (<2s)"],
                  [busiestHour(data.heatmap), "busiest hour"],
                ] as [string, string][]
              ).map(([val, label]) => (
                <div className="dash-card" key={label}>
                  <div className="dash-num">{val}</div>
                  <div className="dash-label">{label}</div>
                </div>
              ))}
            </div>

            {data.totals.count === 0 ? (
              <div className="dash-empty">No transmissions in this window.</div>
            ) : (
              <>
                <h4>When it&apos;s busy</h4>
                <Heatmap heat={data.heatmap} />

                <h4>Top talkers</h4>
                <div className="leaderboard">
                  {data.talkers.length === 0 ? (
                    <div className="dash-empty">No identified voices yet.</div>
                  ) : (
                    (() => {
                      const max = Math.max(...data.talkers.map((t) => t.airtime_ms), 1);
                      return data.talkers.map((t) => (
                        <div
                          className="lb-row"
                          key={t.id}
                          title="Filter the feed to this voice"
                          onClick={() => pick(t.id)}
                        >
                          <span className={"lb-name" + (t.is_named ? "" : " muted")}>{t.label}</span>
                          <div className="lb-track">
                            <div
                              className={"lb-bar" + (t.is_named ? "" : " unnamed")}
                              style={{ width: Math.max(2, Math.round((100 * t.airtime_ms) / max)) + "%" }}
                            />
                          </div>
                          <span className="lb-meta">{`${fmtAirtime(t.airtime_ms)} · ${t.count}`}</span>
                        </div>
                      ));
                    })()
                  )}
                </div>

                {data.by_hub && data.by_hub.length > 0 && (
                  <>
                    <h4>By hub</h4>
                    <div className="leaderboard">
                      {(() => {
                        const max = Math.max(...data.by_hub.map((x) => x.airtime_ms), 1);
                        return data.by_hub.map((x) => (
                          <div className="lb-row" key={x.hub} style={{ cursor: "default" }}>
                            <span className="lb-name">{names[x.hub] || `hub ${x.hub}`}</span>
                            <div className="lb-track">
                              <div
                                className="lb-bar"
                                style={{ width: Math.max(2, Math.round((100 * x.airtime_ms) / max)) + "%" }}
                              />
                            </div>
                            <span className="lb-meta">{`${fmtAirtime(x.airtime_ms)} · ${x.count}`}</span>
                          </div>
                        ));
                      })()}
                    </div>
                  </>
                )}

                {data.by_node && data.by_node.length > 0 && (
                  <>
                    <h4>By node / mode</h4>
                    <div className="leaderboard">
                      {(() => {
                        const max = Math.max(...data.by_node.map((x) => x.airtime_ms), 1);
                        return data.by_node.map((x) => (
                          <div
                            className="lb-row"
                            key={x.node}
                            title="Filter the feed to this node"
                            onClick={() => {
                              closeModal();
                              setFilter("origin", x.node);
                            }}
                          >
                            <span className="lb-name">
                              {names[x.node] ? `${names[x.node]} · ${x.node}` : `node ${x.node}`}
                            </span>
                            <div className="lb-track">
                              <div
                                className="lb-bar"
                                style={{ width: Math.max(2, Math.round((100 * x.airtime_ms) / max)) + "%" }}
                              />
                            </div>
                            <span className="lb-meta">{`${fmtAirtime(x.airtime_ms)} · ${x.count}`}</span>
                          </div>
                        ));
                      })()}
                    </div>
                  </>
                )}

                {data.trend && data.trend.length > 1 && (
                  <>
                    <h4>Daily activity</h4>
                    {(() => {
                      const trend = data.trend.slice(-90);
                      const max = Math.max(...trend.map((d) => d.count), 1);
                      return (
                        <div className="trend">
                          {trend.map((d) => (
                            <div className="trend-col" key={d.day}>
                              <div
                                className="trend-bar"
                                title={`${d.day} — ${d.count} tx`}
                                style={{ height: Math.max(3, Math.round((100 * d.count) / max)) + "%" }}
                              />
                            </div>
                          ))}
                        </div>
                      );
                    })()}
                  </>
                )}
              </>
            )}
          </>
        )}
      </div>
    </SqModal>
  );
}
