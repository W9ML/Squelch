"use client";

import { useEffect, useRef, useState } from "react";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { api } from "@/lib/api";
import { dtmfSummary } from "@/lib/dtmf";
import { findCallSpan } from "@/lib/callspan";
import { fmtTime, nameColor } from "@/lib/format";
import type { DtmfPress, MdcEntry, Transmission } from "@/lib/types";
import { useApp } from "@/state/app-context";
import { ICONS, MotoIcon, SignalBars } from "./icons";
import { Avatar } from "./Avatar";
import { WaveformPlayer } from "./WaveformPlayer";
import { Karaoke } from "./Karaoke";
import { VoterPanel } from "./VoterPanel";

const nodeCache = new Map<string, Promise<{ callsign?: string; description?: string; location?: string } | null>>();
function resolveNode(num: string) {
  if (!nodeCache.has(num)) nodeCache.set(num, api.node(num).catch(() => null));
  return nodeCache.get(num)!;
}

async function downloadMp3(txId: number) {
  try {
    const res = await fetch(api.mp3Url(txId), { credentials: "same-origin" });
    if (!res.ok) {
      let d = res.statusText;
      try {
        d = (await res.json()).detail || d;
      } catch {}
      throw new Error(d);
    }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `tx_${txId}.mp3`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  } catch (e) {
    alert("MP3 download failed: " + (e as Error).message);
  }
}

function OriginBadge({ tx }: { tx: Transmission }) {
  const { status, setFilter } = useApp();
  const local = tx.origin === "local";
  const [text, setText] = useState(
    local ? `${status?.callsign || "local"} repeater` : `via node ${tx.origin}`,
  );
  const [title, setTitle] = useState(
    local
      ? "heard on the repeater's own receiver (RF)"
      : `arrived over the link from connected node ${tx.origin}`,
  );

  useEffect(() => {
    if (local || !tx.origin) return;
    resolveNode(tx.origin).then((info) => {
      if (!info || !info.callsign) return;
      setText(`via node ${tx.origin} (${info.callsign})`);
      const extra = [info.description, info.location].filter(Boolean).join(", ");
      setTitle(
        `arrived over the link from node ${tx.origin} — ${info.callsign}` +
          (extra ? ` (${extra})` : ""),
      );
    });
  }, [local, tx.origin]);

  return (
    <span
      className={"origin-badge filterable " + (local ? "origin-local" : "origin-remote")}
      title={title}
      onClick={(e) => {
        e.stopPropagation();
        if (tx.origin) setFilter("origin", tx.origin);
      }}
    >
      {text}
    </span>
  );
}

/** which AC4TN hub a node's traffic arrived through (610750/751/752), shown
 *  next to the origin so a leaf node reads e.g. "via node 50015 · TACS HUB". */
function HubBadge({ hub }: { hub: string }) {
  const [label, setLabel] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    resolveNode(hub).then((info) => {
      if (live) setLabel(info?.callsign || null);
    });
    return () => {
      live = false;
    };
  }, [hub]);
  return (
    <span className="hub-badge" title={`arrived through hub ${hub}${label ? ` — ${label}` : ""}`}>
      {label || hub}
    </span>
  );
}

function MdcBadge({ m }: { m: MdcEntry }) {
  const { isAdmin, mdcUnits, setFilter, openModal } = useApp();
  const id = m.unit_raw != null ? m.unit_raw : m.unit_id_hex;
  const op = m.unit_raw != null ? mdcUnits[m.unit_raw] || null : null;
  let title: string;
  if (m.source === "node") {
    title =
      (op ? `unit ${id} = ${op}\n` : "") +
      `MDC-1200 ${m.label}, unit ${id}` +
      (m.node ? ` — decoded by node ${m.node}` : "");
  } else {
    title = `MDC-1200 op 0x${(m.op || 0).toString(16).padStart(2, "0")} unit ${m.unit_id}`;
  }
  const filterable = m.unit_raw != null;
  if (filterable && isAdmin) title += "\n(click to link this unit to an operator)";

  return (
    <span
      className={"mdc-badge" + (op ? " mdc-linked" : "") + (filterable ? " filterable" : "")}
      title={title}
      onClick={
        filterable
          ? (e) => {
              e.stopPropagation();
              if (isAdmin) openModal({ kind: "mdcLink", unit: m.unit_raw!, currentOp: op });
              else setFilter("mdc_unit", m.unit_raw!);
            }
          : undefined
      }
    >
      <MotoIcon />
      {op ? `${op} · ${id}` : `${m.label} · ${id}`}
    </span>
  );
}

/** compact 3×4 keypad glyph for the DTMF badge */
function KeypadIcon() {
  return (
    <svg className="keypad-ico" viewBox="0 0 24 24" aria-hidden="true">
      {[0, 1, 2].map((c) =>
        [0, 1, 2, 3].map((r) => (
          <circle key={`${c}${r}`} cx={5 + c * 7} cy={3.5 + r * 5.7} r="2" fill="currentColor" />
        )),
      )}
    </svg>
  );
}

function DtmfBadge({ presses }: { presses: DtmfPress[] }) {
  const digits = presses.map((p) => p.d).join("");
  const summary = dtmfSummary(digits);
  const title =
    "DTMF control tones\n" +
    presses.map((p) => `${p.d} @ ${p.t.toFixed(1)}s`).join("   ");
  return (
    <span className="dtmf-badge" title={title}>
      <KeypadIcon />
      {summary && summary !== digits ? `${digits} — ${summary}` : digits}
    </span>
  );
}

/** Say Again: callsigns cross-checked against voiceprint / self-ID / QRZ.
 *  Corrected calls show heard→resolved; unverified ones a "?"; each has a
 *  "⟳" that replays just that callsign's audio span. */
function ResolvedCallsigns({ tx, bus }: { tx: Transmission; bus: EventTarget }) {
  const { openModal } = useApp();
  const list = tx.callsigns_resolved || [];
  if (!list.length) return null;
  const loop = (heard: string) => {
    const span = findCallSpan(tx.words, heard);
    if (span) bus.dispatchEvent(new CustomEvent("loopspan", { detail: { s: span[0], e: span[1] } }));
  };
  return (
    <div className="cs-row" title="Callsigns — Say Again cross-checks Whisper against the voiceprint, MDC, and QRZ">
      <FontAwesomeIcon className="cs-ico" icon={ICONS.id} />
      {list.map((rc, i) => {
        const canLoop = !!(tx.has_audio && findCallSpan(tx.words, rc.heard));
        const src = rc.sources && rc.sources.length ? " · via " + rc.sources.join(" + ") : "";
        const tip =
          rc.status === "corrected"
            ? `Corrected from "${rc.heard}" — ${rc.spell}${src}`
            : rc.status === "uncertain"
              ? `Heard "${rc.heard}"${rc.alt ? ` — maybe ${rc.alt}?` : ""} — ${rc.spell}`
              : rc.status === "unverified"
                ? `Unverified — ${rc.spell} — not found in QRZ`
                : `${rc.spell}${src}`;
        return (
          <span key={rc.resolved + "-" + i} className={"cs-chip cs-" + rc.status} title={tip}>
            {rc.status === "corrected" && <span className="cs-was">{rc.heard}→</span>}
            <button className="cs-call" onClick={() => openModal({ kind: "callsign", cs: rc.resolved })}>
              {rc.resolved}
            </button>
            {rc.status === "corrected" && <span className="cs-badge ok" aria-hidden>✓</span>}
            {(rc.status === "uncertain" || rc.status === "unverified") && (
              <span className="cs-badge q" aria-hidden>?</span>
            )}
            {rc.status === "uncertain" && rc.alt && (
              <button
                className="cs-alt"
                title={`Look up ${rc.alt}`}
                onClick={() => openModal({ kind: "callsign", cs: rc.alt! })}
              >
                {rc.alt}?
              </button>
            )}
            {canLoop && (
              <button
                className="cs-loop"
                title="Say again — replay just this callsign"
                aria-label="Replay callsign"
                onClick={() => loop(rc.heard)}
              >
                ⟳
              </button>
            )}
          </span>
        );
      })}
    </div>
  );
}

export function TransmissionCard({
  tx,
  onDeleted,
  selectable = false,
  selected = false,
  onToggleSelect,
  removing = false,
  provisional,
}: {
  tx: Transmission;
  onDeleted?: (id: number) => void;
  /** admin bulk-delete: show a checkbox and report toggles upward.
   *  shiftKey is passed through so the feed can do range-select. */
  selectable?: boolean;
  selected?: boolean;
  onToggleSelect?: (id: number, shiftKey?: boolean) => void;
  /** deletion in progress: collapse smoothly before the row unmounts */
  removing?: boolean;
  /** live caption text from the just-ended over, shown while transcription
   *  is still running so the words don't flash to a shimmer */
  provisional?: string;
}) {
  const { isAdmin, status, speakers, setFilter, openModal, watchedSpeakerId, toggleSpeakerWatch } =
    useApp();
  const bus = useRef<EventTarget>(new EventTarget()).current;
  const [reprocessing, setReprocessing] = useState(false);
  const [secondOp, setSecondOp] = useState(false);
  const [playing, setPlaying] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const [shared, setShared] = useState(false);

  const shareTx = async (e: React.MouseEvent) => {
    e.stopPropagation();
    const url = `${location.origin}/?tx=${tx.id}#tx-${tx.id}`;
    const title = `Squelch — ${label || "transmission"} at ${fmtTime(tx.started_at)}`;
    if (navigator.share) {
      // phones: the native share sheet is its own feedback
      try {
        await navigator.share({ title, url });
        return;
      } catch {
        return; // user dismissed the sheet
      }
    }
    try {
      await navigator.clipboard.writeText(url);
      setShared(true);
      setTimeout(() => setShared(false), 1600);
    } catch {
      /* clipboard unavailable (http + old browser) — nothing to do */
    }
  };
  const menuRef = useRef<HTMLDivElement>(null);
  const cardRef = useRef<HTMLDivElement>(null);


  // collapse the card in place so its neighbors slide into the gap — an
  // instant unmount shrinks scrollHeight in one frame and the resulting
  // scroll clamp visibly yanks the whole feed when pinned at the bottom
  useEffect(() => {
    if (!removing) return;
    const el = cardRef.current;
    if (!el) return;
    el.style.height = el.offsetHeight + "px";
    el.style.overflow = "hidden";
    el.style.pointerEvents = "none";
    void el.offsetHeight; // commit the start height, or the transition no-ops
    el.style.transition =
      "height .22s ease, opacity .18s ease, margin .22s ease, padding .22s ease, border-width .22s ease";
    el.style.height = "0px";
    el.style.opacity = "0";
    el.style.marginTop = el.style.marginBottom = "0px";
    el.style.paddingTop = el.style.paddingBottom = "0px";
    el.style.borderWidth = "0";
  }, [removing]);

  const label =
    tx.speaker_id != null
      ? speakers.get(tx.speaker_id) || tx.speaker_label || `Speaker ${tx.speaker_id}`
      : null;

  // card gets a `playing` class off the player's explicit play/pause events —
  // NOT "ptime", which also fires for paused seeks and left the highlight
  // stuck on cards that were paused by another card starting
  useEffect(() => {
    const onPlay = () => setPlaying(true);
    const onStop = () => setPlaying(false);
    bus.addEventListener("pplay", onPlay);
    bus.addEventListener("ppause", onStop);
    bus.addEventListener("pended", onStop);
    return () => {
      bus.removeEventListener("pplay", onPlay);
      bus.removeEventListener("ppause", onStop);
      bus.removeEventListener("pended", onStop);
    };
  }, [bus]);

  // close the "more actions" menu on outside click / Escape
  useEffect(() => {
    if (!menuOpen) return;
    const onDoc = (e: PointerEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) setMenuOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setMenuOpen(false);
    };
    document.addEventListener("pointerdown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("pointerdown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [menuOpen]);

  const nameClick = () => {
    if (tx.speaker_id != null) setFilter("speaker_id", tx.speaker_id);
    else if (isAdmin) openModal({ kind: "speaker", tx });
  };
  const nameClickable = tx.speaker_id != null || isAdmin;

  return (
    <div
      ref={cardRef}
      id={`tx-${tx.id}`}
      className={"card" + (playing ? " playing" : "") + (selected ? " selected" : "")}
      data-id={tx.id}
    >
      {selectable && (
        <input
          type="checkbox"
          className="sel-box"
          checked={selected}
          title="Select for bulk delete — Shift-click to select a range"
          onChange={() => {}}
          onClick={(e) => {
            e.stopPropagation();
            onToggleSelect?.(tx.id, e.shiftKey);
          }}
        />
      )}
      {label ? (
        <Avatar
          label={label}
          className={nameClickable ? "clickable" : ""}
          title={tx.speaker_id != null ? "show only this speaker" : "click to name this voice"}
          onClick={nameClickable ? nameClick : undefined}
        />
      ) : (
        <div
          className={"avatar unknown" + (isAdmin ? " clickable" : "")}
          title={isAdmin ? "click to name this voice" : undefined}
          onClick={isAdmin ? nameClick : undefined}
        >
          ?
        </div>
      )}

      <div className="card-main">
        <div className="card-head">
          {label ? (
            <span
              className={"speaker-name" + (nameClickable ? " clickable" : "")}
              style={{ color: nameColor(label) }}
              title={tx.speaker_id != null ? "show only this speaker" : "click to name this voice"}
              onClick={nameClickable ? nameClick : undefined}
            >
              {label}
            </span>
          ) : (
            <span className="speaker-name unknown">
              {tx.status === "done" ? "unknown voice" : "identifying…"}
            </span>
          )}

          {isAdmin && tx.speaker_id != null && label && (
            <button
              className={"star-btn" + (watchedSpeakerId(tx.speaker_id) ? " on" : "")}
              title={
                watchedSpeakerId(tx.speaker_id)
                  ? `Stop alerting me when ${label} is on the air`
                  : `Alert me when ${label} is on the air`
              }
              aria-label="Watch this operator"
              onClick={(e) => {
                e.stopPropagation();
                const sid = tx.speaker_id!;
                const on = !!watchedSpeakerId(sid);
                openModal({
                  kind: "confirm",
                  title: on ? "Remove alert?" : "Add alert?",
                  message: on
                    ? `Stop alerting you when ${label} is on the air?`
                    : `Alert you whenever ${label} is on the air?`,
                  confirmLabel: on ? "Remove alert" : "Add alert",
                  danger: on,
                  onConfirm: () => toggleSpeakerWatch(sid, label),
                });
              }}
            >
              <FontAwesomeIcon icon={ICONS.star} />
            </button>
          )}

          {tx.speaker_score != null && tx.speaker_score < 1 && (
            <span
              className="match"
              title={
                tx.speaker_verified === "voice"
                  ? "voice match confidence"
                  : `identified by ${tx.speaker_verified || "voice"} (voice ${Math.round(
                      tx.speaker_score * 100,
                    )}%)`
              }
            >
              {Math.round(tx.speaker_score * 100)}%
            </span>
          )}

          {tx.speaker_id == null && tx.suggest_speaker_id != null && tx.suggest_label && (
            <button
              className={"suggest-chip" + (isAdmin ? " clickable" : "")}
              title={isAdmin ? "confirm this is " + tx.suggest_label : "possible match — not confirmed"}
              onClick={
                isAdmin
                  ? async (e) => {
                      e.stopPropagation();
                      try {
                        await api.assignSpeaker(tx.id, { speaker_id: tx.suggest_speaker_id! });
                      } catch (err) {
                        alert("Confirm failed: " + (err as Error).message);
                      }
                    }
                  : undefined
              }
            >
              <span className="suggest-q">?</span>
              {/* a callsign-bootstrap suggestion has no cosine — showing
                  "0%" next to a name we literally just heard reads as
                  broken, so only voice-scored chips get a percentage */}
              {tx.suggest_score
                ? `possible: ${tx.suggest_label} · ${Math.round(tx.suggest_score * 100)}%`
                : `possible: ${tx.suggest_label} — tap to confirm`}
            </button>
          )}

          {(tx.mdc || []).map((m, i) => (
            <MdcBadge key={i} m={m} />
          ))}

          {tx.dtmf && tx.dtmf.length > 0 && <DtmfBadge presses={tx.dtmf} />}

          {tx.origin && <OriginBadge tx={tx} />}
          {tx.origin_hub && <HubBadge hub={tx.origin_hub} />}

          {tx.quality && (
            <span
              className={`qual qual-${tx.quality.label}`}
              title={
                `signal ~${tx.quality.snr} dB SNR (audio-domain estimate)` +
                (tx.quality.clipping ? " — clipping" : "") +
                (tx.quality.flutter && tx.quality.flutter > 0.85 ? " — mobile flutter" : "")
              }
            >
              <SignalBars snr={tx.quality.snr} /> {tx.quality.label}
            </span>
          )}

          {tx.transcript_model?.startsWith("elevenlabs") && (
            <span
              className="el-badge"
              title="Transcript from ElevenLabs (cloud second opinion), not the local model"
            >
              ElevenLabs
            </span>
          )}

          <span className="head-right">
            <span title={new Date(tx.started_at * 1000).toLocaleString()}>
              {fmtTime(tx.started_at)}
            </span>
            {tx.has_embedding && (
              <button
                className="card-action"
                title={
                  tx.speaker_id != null
                    ? "Find transmissions with a similar voice"
                    : "Unknown voice — find other overs that sound like this one"
                }
                onClick={() => openModal({ kind: "similar", tx })}
              >
                <FontAwesomeIcon icon={ICONS.similar} />
              </button>
            )}
            <button
              className="card-action"
              title={shared ? "Link copied!" : "Share a link to this recording"}
              onClick={shareTx}
            >
              <FontAwesomeIcon icon={shared ? ICONS.check : ICONS.share} />
            </button>
            {isAdmin && (
              <>
                <div className="card-menu-wrap" ref={menuRef}>
                  <button
                    className={"card-action" + (menuOpen ? " active" : "") + (reprocessing ? " spin-icon" : "")}
                    title="More actions"
                    aria-haspopup="menu"
                    aria-expanded={menuOpen}
                    onClick={() => setMenuOpen((o) => !o)}
                  >
                    <FontAwesomeIcon icon={reprocessing ? ICONS.reprocess : ICONS.more} />
                  </button>
                  {menuOpen && (
                    <div className="card-menu" role="menu">
                      {status?.can_settings && (
                        <button
                          className="card-menu-item"
                          role="menuitem"
                          onClick={() => {
                            setMenuOpen(false);
                            openModal({ kind: "addToCase", tx });
                          }}
                        >
                          <FontAwesomeIcon icon={ICONS.addCase} /> Add to case
                        </button>
                      )}
                      {tx.speaker_id != null && (
                        <button
                          className="card-menu-item"
                          role="menuitem"
                          onClick={() => {
                            setMenuOpen(false);
                            openModal({ kind: "speaker", tx });
                          }}
                        >
                          <FontAwesomeIcon icon={ICONS.edit} /> Rename / reassign
                        </button>
                      )}
                      {tx.has_audio && (
                        <>
                          <button
                            className="card-menu-item"
                            role="menuitem"
                            disabled={reprocessing}
                            onClick={async () => {
                              setMenuOpen(false);
                              setReprocessing(true);
                              try {
                                await api.reprocess(tx.id);
                              } catch (e) {
                                alert("Reprocess failed: " + (e as Error).message);
                              } finally {
                                setTimeout(() => setReprocessing(false), 1500);
                              }
                            }}
                          >
                            <FontAwesomeIcon icon={ICONS.reprocess} /> Reprocess
                          </button>
                          {status?.has_elevenlabs && (
                            <button
                              className="card-menu-item"
                              role="menuitem"
                              disabled={secondOp}
                              title="Re-transcribe this over via ElevenLabs (cloud) — sends only this recording"
                              onClick={async () => {
                                setMenuOpen(false);
                                setSecondOp(true);
                                try {
                                  await api.secondOpinion(tx.id);
                                } catch (e) {
                                  alert("Second opinion failed: " + (e as Error).message);
                                } finally {
                                  setTimeout(() => setSecondOp(false), 1500);
                                }
                              }}
                            >
                              <FontAwesomeIcon icon={ICONS.reprocess} /> Second opinion (ElevenLabs)
                            </button>
                          )}
                          <button
                            className="card-menu-item"
                            role="menuitem"
                            onClick={() => {
                              setMenuOpen(false);
                              downloadMp3(tx.id);
                            }}
                          >
                            <FontAwesomeIcon icon={ICONS.download} /> Download MP3
                          </button>
                        </>
                      )}
                    </div>
                  )}
                </div>
                <button
                  className="card-action danger"
                  title="Delete this recording"
                  onClick={() =>
                    openModal({
                      kind: "confirm",
                      title: "Delete recording?",
                      message: `Delete this ${(tx.duration_ms / 1000).toFixed(
                        1,
                      )}s recording? This can't be undone.`,
                      confirmLabel: "Delete",
                      danger: true,
                      onConfirm: async () => {
                        try {
                          await api.deleteTransmission(tx.id);
                          onDeleted?.(tx.id);
                        } catch (e) {
                          alert("Delete failed: " + (e as Error).message);
                        }
                      },
                    })
                  }
                >
                  <FontAwesomeIcon icon={ICONS.trash} />
                </button>
              </>
            )}
          </span>
        </div>

        {tx.status === "processing" ? (
          provisional ? (
            <div className="transcript settling">
              {provisional}
              <span className="live-caret" />
            </div>
          ) : (
            <div className="shimmer" />
          )
        ) : tx.transcript && tx.words && tx.words.length ? (
          <Karaoke words={tx.words} bus={bus} />
        ) : tx.transcript ? (
          <div className="transcript">{tx.transcript}</div>
        ) : (
          <div className="transcript none">
            {tx.status === "error"
              ? "processing failed"
              : tx.transcript_model === "storm-skipped"
                ? "skipped — repeater storm (admin: Reprocess to transcribe)"
                : tx.mdc && tx.mdc.length
                  ? "MDC data burst — no voice"
                  : "no speech recognized"}
          </div>
        )}

        {status?.has_sayagain && tx.callsigns_resolved && tx.callsigns_resolved.length > 0 ? (
          <ResolvedCallsigns tx={tx} bus={bus} />
        ) : tx.callsigns && tx.callsigns.length > 0 ? (
          <div className="cs-row" title="Callsigns heard — click to look up">
            <FontAwesomeIcon className="cs-ico" icon={ICONS.id} />
            {tx.callsigns.map((cs) => (
              <button key={cs} className="cs-chip" onClick={() => openModal({ kind: "callsign", cs })}>
                {cs}
              </button>
            ))}
          </div>
        ) : null}

        {(tx.has_audio || (tx.peaks && tx.peaks.length > 0)) && (
          <WaveformPlayer tx={tx} bus={bus} />
        )}
        {/* voter RSSI is login-gated (it feeds the admin-only geolocation),
            so gate its panel on login state too — otherwise the already-
            embedded voter data lingers on cards loaded before logout */}
        {isAdmin && tx.voter && tx.voter.nodes && tx.voter.nodes.length > 0 && (
          <VoterPanel tx={tx} bus={bus} />
        )}
      </div>
    </div>
  );
}
