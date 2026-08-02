"use client";

import { useEffect, useState } from "react";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { api } from "@/lib/api";
import { THEME_LABELS } from "@/lib/format";
import type { Status } from "@/lib/types";
import { useApp } from "@/state/app-context";
import { BrandMark, ICONS } from "./icons";
import { SoundControl } from "./SoundControl";
import { LiveControl } from "./LiveControl";
import { AccountMenu } from "./AccountMenu";

function statusPill(
  wsOpen: boolean,
  wsConnected: boolean,
  rxActive: boolean,
  status: Status | null,
) {
  if (!wsOpen) return { cls: "down", text: "offline", title: "not connected to the server" };
  if (!wsConnected)
    return { cls: "reconnecting", text: "reconnecting", title: "lost the live feed — reconnecting…" };
  if (rxActive) return { cls: "on-air", text: "on air", title: "the node is receiving right now" };
  // connected to the server but the node has never sent audio — visibly
  // distinct from a healthy-but-idle system so setup problems aren't hidden
  // behind a reassuring green light
  if (!status?.last_frame)
    return {
      cls: "no-audio",
      text: "no audio yet",
      title: "connected to the server, but no audio has arrived from your node yet",
    };
  const mins = Math.max(0, Math.round((Date.now() / 1000 - status.last_frame) / 60));
  const ago = mins < 60 ? `${mins} min ago` : `${Math.round(mins / 60)} h ago`;
  return { cls: "ok", text: "listening", title: `connected — last audio ${ago}` };
}

export function Header() {
  const {
    status,
    isAdmin,
    canSettings,
    connections,
    theme,
    setLocalTheme,
    bandscopeOn,
    toggleBandscope,
    wsOpen,
    wsConnected,
    rxActive,
    paused,
    togglePaused,
    openModal,
    searching,
    searchText,
    setSearch,
    goHome,
    refreshStatus,
  } = useApp();

  const [q, setQ] = useState(searchText);
  useEffect(() => {
    setQ(searchText);
  }, [searchText]);

  const cs = status?.brand_callsign || status?.callsign;
  const node = status?.brand_node || status?.node_number;
  const nodeLine = status?.node_label || (node ? `Node ${node}` : null);
  const sub = [cs, nodeLine].filter(Boolean).join(" · ");
  const pill = statusPill(wsOpen, wsConnected, rxActive, status);
  const themes = status?.themes || [];

  return (
    <header id="topbar">
      <div className="topbar-left">
        <div
          className="brand"
          role="button"
          tabIndex={0}
          title="Home — clear filters and jump to the latest recording"
          aria-label="Home — clear filters and jump to the latest recording"
          onClick={goHome}
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === " ") {
              e.preventDefault();
              goHome();
            }
          }}
        >
          <span id="logo-slot">
            {status?.logo_ts ? (
              // eslint-disable-next-line @next/next/no-img-element
              <img src={api.logoUrl(status.logo_ts)} alt="logo" />
            ) : (
              <BrandMark />
            )}
          </span>
          <div className="brand-text">
            <span className="brand-name">{status?.site_name || "Squelch"}</span>
            <span className="brand-sub">{sub}</span>
          </div>
        </div>

        {/* live connection count — super/admin only; click opens the log */}
        {canSettings && (
          <button
            className="conn-count"
            title={`${connections} active connection${connections === 1 ? "" : "s"} — click for the connection log`}
            aria-label={`${connections} active connections — open connection log`}
            onClick={() => openModal({ kind: "connlog" })}
          >
            <FontAwesomeIcon icon={ICONS.users} />
            <span className="conn-n">{connections}</span>
          </button>
        )}
      </div>

      <div className="controls">
        {!!status?.queue_depth && (
          <span className="chip" title="transmissions waiting to be processed">
            {`processing ${status.queue_depth}`}
          </span>
        )}

        {canSettings && status?.models && (
          <select
            className={"native-select" + (status.whisper_loading ? " loading" : "")}
            title={
              status.whisper_loading
                ? `downloading "${status.whisper_loading}" — the current model stays active until it's ready`
                : "Whisper model"
            }
            value={status.whisper_model}
            onChange={async (e) => {
              const model = e.target.value;
              try {
                await api.setWhisperModel(model);
              } catch (err) {
                alert((err as Error).message);
                refreshStatus();
              }
            }}
          >
            {status.models.map((m) => (
              <option key={m} value={m}>
                {m}
              </option>
            ))}
          </select>
        )}

        {status?.has_bandscope && (
          <button
            className={"scope-toggle" + (bandscopeOn ? " on" : "")}
            title={bandscopeOn ? "Hide the live bandscope" : "Show the live bandscope"}
            aria-label="Toggle bandscope"
            aria-pressed={bandscopeOn}
            onClick={toggleBandscope}
          >
            <svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true">
              <rect x="2" y="11" width="3" height="9" rx="1" fill="currentColor" />
              <rect x="7" y="6" width="3" height="14" rx="1" fill="currentColor" />
              <rect x="12" y="3" width="3" height="17" rx="1" fill="currentColor" />
              <rect x="17" y="8" width="3" height="12" rx="1" fill="currentColor" />
            </svg>
          </button>
        )}

        {themes.length > 0 && (
          <select
            className="native-select"
            title="Theme (saved in this browser)"
            value={theme}
            onChange={(e) => setLocalTheme(e.target.value)}
          >
            {themes.map((t) => (
              <option key={t} value={t}>
                {THEME_LABELS[t] || t}
              </option>
            ))}
          </select>
        )}

        <div className="search-wrap">
          <FontAwesomeIcon className="search-ico" icon={ICONS.search} />
          <input
            id="search"
            type="search"
            placeholder="Search transcripts"
            autoComplete="off"
            value={q}
            onChange={(e) => {
              setQ(e.target.value);
              if (e.target.value === "" && searching) setSearch("");
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter") setSearch(q.trim());
            }}
          />
        </div>

        <button
          className="icon-btn"
          title="Filter transmissions"
          aria-label="Filter transmissions"
          onClick={() => openModal({ kind: "filters" })}
        >
          <FontAwesomeIcon icon={ICONS.filter} />
        </button>
        <button
          className="icon-btn"
          title="Activity dashboard"
          aria-label="Activity dashboard"
          onClick={() => openModal({ kind: "stats" })}
        >
          <FontAwesomeIcon icon={ICONS.stats} />
        </button>
        <button
          className="icon-btn"
          title="Callsign logbook"
          aria-label="Callsign logbook"
          onClick={() => openModal({ kind: "logbook" })}
        >
          <FontAwesomeIcon icon={ICONS.logbook} />
        </button>
        {/* pausing the live feed is for logged-in accounts only */}
        {!!status?.username && (
          <button
            className={"icon-btn" + (paused ? " active" : "")}
            title={paused ? "Resume feed" : "Pause feed"}
            aria-label="Pause feed"
            onClick={togglePaused}
          >
            <FontAwesomeIcon icon={ICONS.pause} />
          </button>
        )}

        <SoundControl />
        <LiveControl />

        <div
          className={"status-pill clickable " + pill.cls}
          title={pill.title + " · click for network status"}
          role="button"
          tabIndex={0}
          onClick={() => openModal({ kind: "network" })}
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === " ") {
              e.preventDefault();
              openModal({ kind: "network" });
            }
          }}
        >
          <span className="dot" />
          <span className="status-text">{pill.text}</span>
        </div>

        {isAdmin ? (
          <AccountMenu />
        ) : (
          <button
            className="text-btn login-btn"
            title="Admin login"
            onClick={() => openModal({ kind: "login" })}
          >
            Login
          </button>
        )}
      </div>
    </header>
  );
}
