"use client";

import { useEffect, useMemo, useState } from "react";
import { api } from "@/lib/api";
import { DOW } from "@/lib/format";
import type { Connection, ConnStats } from "@/lib/types";
import { useApp } from "@/state/app-context";
import { SqModal } from "./SqModal";

type StatusFilter = "all" | "active" | "closed";
type Tab = "overview" | "log";

// analytics windows: [key, label, days] (0 = all time)
const RANGES: [string, string, number][] = [
  ["1", "24 hrs", 1],
  ["7", "7 days", 7],
  ["30", "30 days", 30],
  ["90", "90 days", 90],
  ["all", "All", 0],
];

function fmtWhen(epoch: number): string {
  return new Date(epoch * 1000).toLocaleString([], {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
    second: "2-digit",
  });
}

function fmtDur(secs: number): string {
  secs = Math.max(0, Math.round(secs));
  if (secs < 60) return `${secs}s`;
  const m = Math.floor(secs / 60);
  if (m < 60) return `${m}m ${secs % 60}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}

// human total viewing time (spans minutes → days)
function fmtView(secs: number): string {
  const h = secs / 3600;
  if (h < 1) return `${Math.round(secs / 60)} min`;
  if (h < 100) return `${h.toFixed(h < 10 ? 1 : 0)} h`;
  return `${Math.round(h / 24)} days`;
}

function download(url: string) {
  const a = document.createElement("a");
  a.href = url;
  a.rel = "noopener";
  document.body.appendChild(a);
  a.click();
  a.remove();
}

function ConnHeatmap({ heat }: { heat: number[][] }) {
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
                title={`${DOW[d]} ${String(h).padStart(2, "0")}:00 — ${v} visit${v === 1 ? "" : "s"}`}
                style={v ? { background: `color-mix(in srgb, var(--accent) ${pct}%, transparent)` } : undefined}
              />
            );
          })}
        </div>
      ))}
    </div>
  );
}

function Bars({ rows }: { rows: { key: string; label: string; value: number; meta: string; muted?: boolean }[] }) {
  const max = Math.max(...rows.map((r) => r.value), 1);
  return (
    <div className="leaderboard">
      {rows.map((r) => (
        <div className="lb-row" key={r.key} style={{ cursor: "default" }}>
          <span className={"lb-name" + (r.muted ? " muted" : "")}>{r.label}</span>
          <div className="lb-track">
            <div
              className="lb-bar"
              style={{ width: Math.max(2, Math.round((100 * r.value) / max)) + "%" }}
            />
          </div>
          <span className="lb-meta">{r.meta}</span>
        </div>
      ))}
    </div>
  );
}

function Overview({ range, setRange }: { range: string; setRange: (r: string) => void }) {
  const [data, setData] = useState<ConnStats | null>(null);
  const [msg, setMsg] = useState("crunching the numbers…");

  useEffect(() => {
    setData(null);
    setMsg("crunching the numbers…");
    const tz = -new Date().getTimezoneOffset();
    const days = RANGES.find(([k]) => k === range)?.[2] ?? 30;
    api.connectionStats(days, tz).then(setData).catch((e) => setMsg((e as Error).message));
  }, [range]);

  return (
    <>
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
      </div>
      <div className="dash">
        {!data ? (
          <div className="dash-loading">{msg}</div>
        ) : data.totals.sessions === 0 ? (
          <div className="dash-empty">No visitors in this window.</div>
        ) : (
          <>
            <div className="dash-cards">
              {(
                [
                  [data.totals.sessions.toLocaleString(), "visits"],
                  [String(data.totals.unique_ips), "unique IPs"],
                  [String(data.totals.unique_users), "logged-in users"],
                  [fmtDur(data.totals.avg_seconds), "avg visit"],
                  [String(data.totals.peak_concurrent), "peak at once"],
                  [fmtView(data.totals.total_seconds), "total view time"],
                ] as [string, string][]
              ).map(([val, label]) => (
                <div className="dash-card" key={label}>
                  <div className="dash-num">{val}</div>
                  <div className="dash-label">{label}</div>
                </div>
              ))}
            </div>

            <h4>When people visit</h4>
            <ConnHeatmap heat={data.heatmap} />

            <h4>Top visitors (by IP)</h4>
            <Bars
              rows={data.top_ips.map((r) => ({
                key: r.ip,
                label: r.ip,
                value: r.sessions,
                meta: `${r.sessions} · ${fmtView(r.seconds)}`,
              }))}
            />

            {data.top_users.length > 0 && (
              <>
                <h4>Top logged-in users</h4>
                <Bars
                  rows={data.top_users.map((r) => ({
                    key: r.username,
                    label: r.username,
                    value: r.sessions,
                    meta: `${r.sessions} · ${fmtView(r.seconds)}`,
                  }))}
                />
              </>
            )}

            <h4>How long they stay</h4>
            <Bars
              rows={data.durations.map((b) => ({
                key: b.label,
                label: b.label,
                value: b.count,
                meta: String(b.count),
              }))}
            />

            {data.clients.length > 0 && (
              <>
                <h4>Clients</h4>
                <Bars
                  rows={data.clients.map((c) => ({
                    key: c.name,
                    label: c.name,
                    value: c.count,
                    meta: String(c.count),
                    muted: c.name === "unknown" || c.name === "Other",
                  }))}
                />
              </>
            )}

            {data.trend.length > 1 && (
              <>
                <h4>Daily visits</h4>
                {(() => {
                  const trend = data.trend.slice(-90);
                  const max = Math.max(...trend.map((d) => d.count), 1);
                  return (
                    <div className="trend">
                      {trend.map((d) => (
                        <div className="trend-col" key={d.day}>
                          <div
                            className="trend-bar"
                            title={`${d.day} — ${d.count} visit${d.count === 1 ? "" : "s"}`}
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
      </div>
    </>
  );
}

function LogView({ search, setSearch, status, setStatus, rows, msg }: {
  search: string; setSearch: (s: string) => void;
  status: StatusFilter; setStatus: (s: StatusFilter) => void;
  rows: Connection[] | null; msg: string;
}) {
  const now = Date.now() / 1000;
  const shown = useMemo(() => {
    if (!rows) return [];
    const q = search.trim().toLowerCase();
    return rows
      .filter((c) => {
        const open = c.disconnected_at == null;
        if (status === "active" && !open) return false;
        if (status === "closed" && open) return false;
        if (!q) return true;
        return (
          (c.ip || "").toLowerCase().includes(q) ||
          (c.username || "").toLowerCase().includes(q) ||
          (c.user_agent || "").toLowerCase().includes(q)
        );
      })
      .sort((a, b) => {
        const ao = a.disconnected_at == null ? 1 : 0;
        const bo = b.disconnected_at == null ? 1 : 0;
        return bo - ao || b.connected_at - a.connected_at;
      });
  }, [rows, search, status, now]);

  return (
    <>
      <div className="conn-tools">
        <input
          className="conn-search"
          type="search"
          placeholder="Search IP, user, or device…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <div className="conn-seg" role="group" aria-label="Filter by status">
          {(["all", "active", "closed"] as StatusFilter[]).map((s) => (
            <button
              key={s}
              className={"conn-seg-btn" + (status === s ? " on" : "")}
              onClick={() => setStatus(s)}
            >
              {s === "all" ? "All" : s === "active" ? "Active" : "Closed"}
            </button>
          ))}
        </div>
      </div>

      {!rows ? (
        <div className="conn-empty">{msg}</div>
      ) : shown.length === 0 ? (
        <div className="conn-empty">no connections match.</div>
      ) : (
        <div className="conn-table-wrap">
          <table className="conn-table">
            <thead>
              <tr>
                <th>IP</th>
                <th>User</th>
                <th>Joined</th>
                <th>Left</th>
                <th>Duration</th>
              </tr>
            </thead>
            <tbody>
              {shown.map((c) => {
                const open = c.disconnected_at == null;
                const dur = (open ? now : c.disconnected_at!) - c.connected_at;
                return (
                  <tr key={c.id} className={open ? "conn-open" : undefined}>
                    <td className="conn-ip" title={c.user_agent || undefined}>
                      {c.ip || "—"}
                    </td>
                    <td>{c.username || <span className="muted">anon</span>}</td>
                    <td>{fmtWhen(c.connected_at)}</td>
                    <td>
                      {open ? (
                        <span className="conn-live-badge">● active</span>
                      ) : (
                        fmtWhen(c.disconnected_at!)
                      )}
                    </td>
                    <td>{fmtDur(dur)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}

export function ConnLogModal() {
  const { closeModal } = useApp();
  const [tab, setTab] = useState<Tab>("overview");
  const [range, setRange] = useState("30");
  // log tab state
  const [rows, setRows] = useState<Connection[] | null>(null);
  const [live, setLive] = useState<number | null>(null);
  const [msg, setMsg] = useState("loading…");
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState<StatusFilter>("all");

  // load the raw log the first time the Log tab is opened
  useEffect(() => {
    if (tab !== "log" || rows !== null) return;
    api
      .connections(500)
      .then(({ connections, live }) => {
        setRows(connections);
        setLive(live);
        if (!connections.length) setMsg("no connections recorded yet.");
      })
      .catch((e) => setMsg((e as Error).message));
  }, [tab, rows]);

  const exportUrl = (fmt: "csv" | "xlsx") =>
    api.connectionsExportUrl(fmt, search.trim(), status);

  const shownCount = useMemo(() => {
    if (!rows) return 0;
    const q = search.trim().toLowerCase();
    return rows.filter((c) => {
      const open = c.disconnected_at == null;
      if (status === "active" && !open) return false;
      if (status === "closed" && open) return false;
      if (!q) return true;
      return (
        (c.ip || "").toLowerCase().includes(q) ||
        (c.username || "").toLowerCase().includes(q) ||
        (c.user_agent || "").toLowerCase().includes(q)
      );
    }).length;
  }, [rows, search, status]);

  return (
    <SqModal
      title="Connections"
      wide
      onClose={closeModal}
      footer={
        tab === "log" ? (
          <>
            <span className="conn-foot muted">
              {rows ? `${shownCount} shown` : ""}
              {live != null ? ` · ${live} active now` : ""}
            </span>
            <button className="text-btn" onClick={() => download(exportUrl("csv"))}>
              Export CSV
            </button>
            <button className="text-btn" onClick={() => download(exportUrl("xlsx"))}>
              Export XLSX
            </button>
            <button className="text-btn primary" onClick={closeModal}>
              Close
            </button>
          </>
        ) : (
          <button className="text-btn primary" onClick={closeModal}>
            Close
          </button>
        )
      }
    >
      <div className="seg conn-tabs">
        <button
          className={"seg-btn" + (tab === "overview" ? " on" : "")}
          onClick={() => setTab("overview")}
        >
          Overview
        </button>
        <button
          className={"seg-btn" + (tab === "log" ? " on" : "")}
          onClick={() => setTab("log")}
        >
          Log
        </button>
      </div>

      {tab === "overview" ? (
        <Overview range={range} setRange={setRange} />
      ) : (
        <LogView
          search={search}
          setSearch={setSearch}
          status={status}
          setStatus={setStatus}
          rows={rows}
          msg={msg}
        />
      )}
    </SqModal>
  );
}
