"use client";

import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { api, type FeedParams } from "@/lib/api";
import { dateToEpoch, dayBounds, dayKey, epochToDateInput, fmtDay } from "@/lib/format";
import type { Transmission, WsEvent } from "@/lib/types";
import { useApp } from "@/state/app-context";
import { useWebSocket } from "@/lib/ws";
import { playNewRecording, playTranscription, playWatchAlert } from "@/lib/sound";
import { showNotification } from "@/lib/notify";
import { requestPlay } from "@/lib/playqueue";
import { TransmissionCard } from "./TransmissionCard";
import { toggleActiveAudio } from "./WaveformPlayer";
import { FilterBar } from "./FilterBar";
import { LiveCard } from "./LiveCard";
import { Bandscope } from "./Bandscope";
import { pushBscope } from "@/lib/bandscope";
import { DtmfOverlay } from "./DtmfOverlay";
import { BrandMark, ICONS } from "./icons";

function beep() {
  try {
    const Ctx = window.AudioContext || (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext;
    const ac = new Ctx();
    const o = ac.createOscillator();
    const g = ac.createGain();
    o.type = "square";
    o.frequency.value = 880;
    g.gain.value = 0.06;
    o.connect(g);
    g.connect(ac.destination);
    o.start();
    o.frequency.setValueAtTime(660, ac.currentTime + 0.15);
    o.stop(ac.currentTime + 0.3);
    o.onended = () => ac.close();
  } catch {
    /* noop */
  }
}

function handleWatchHit(msg: Extract<WsEvent, { type: "watch_hit" }>, sound: boolean) {
  const title = msg.label || "Squelch alert";
  const body = msg.tx?.transcript || msg.reason || "";
  showNotification(title, body, "squelch-watch-" + Date.now());
  if (sound) playWatchAlert();
  if (msg.tx) {
    const c = document.querySelector(`.card[data-id="${msg.tx.id}"]`);
    if (c) {
      c.classList.add("watch-flash");
      setTimeout(() => c.classList.remove("watch-flash"), 4000);
    }
  }
  if (msg.kind === "emergency") beep(); // an extra, more urgent tone on emergencies
}

function DayDivider({ epoch, onPurged }: { epoch: number; onPurged: () => void }) {
  const { isAdmin, openModal } = useApp();
  const purge = async () => {
    const [start, end] = dayBounds(epoch);
    let count: number | string = "";
    try {
      count = (await api.purgeCount(start, end)).count;
    } catch {}
    openModal({
      kind: "confirm",
      title: "Delete this day?",
      message: `Delete all ${count} recordings from ${fmtDay(epoch)}? This can't be undone.`,
      confirmLabel: "Delete all",
      danger: true,
      onConfirm: async () => {
        try {
          await api.purge(start, end);
          onPurged();
        } catch (e) {
          alert("Purge failed: " + (e as Error).message);
        }
      },
    });
  };
  return (
    <div className="day-divider">
      <span>{fmtDay(epoch)}</span>
      {isAdmin && (
        <button className="day-purge" title="Delete all recordings from this day" onClick={purge}>
          <FontAwesomeIcon icon={ICONS.trash} />
        </button>
      )}
    </div>
  );
}

/** First-run hint (admin-only): the server is up but the node has never sent
 *  audio, so point the operator at the node-setup steps. Auto-hides the moment
 *  audio arrives (last_frame set); dismissible for intentional no-node runs. */
function NodeSetupHint({ port }: { port?: number }) {
  const [hidden, setHidden] = useState(false);
  useEffect(() => {
    if (localStorage.getItem("squelch-nodehint-dismissed") === "1") setHidden(true);
  }, []);
  if (hidden) return null;
  const p = port ?? 32001;
  const stanza = `[1999](node-main)
rxchannel = USRP/<VM-IP>:${p}:34001   ; VM IP : Squelch's port : local return port
duplex = 0
hangtime = 0
telemdefault = 0
startup_macro = *2546XX                 ; *2 = monitor your node (receive-only)`;
  return (
    <div className="node-hint">
      <div className="node-hint-head">
        <span>No audio from your node yet</span>
        <span className="spacer" />
        <button
          className="node-hint-dismiss"
          title="Dismiss"
          aria-label="Dismiss"
          onClick={() => {
            localStorage.setItem("squelch-nodehint-dismissed", "1");
            setHidden(true);
          }}
        >
          ×
        </button>
      </div>
      <p>
        Squelch is listening on <code>UDP {p}</code> but hasn&apos;t received anything
        yet. On the Pi (ASL3), add a receive-only pseudo node — three edits across
        two files. It hears everything your node hears but can never key it up:
      </p>
      <ol className="node-hint-steps">
        <li>
          <span className="step-label">
            Enable the USRP driver in <code>/etc/asterisk/modules.conf</code>:
          </span>
          <pre className="node-hint-config"><code>{"load => chan_usrp.so"}</code></pre>
        </li>
        <li>
          <span className="step-label">
            Add the pseudo-node stanza to <code>/etc/asterisk/rpt.conf</code> — swap{" "}
            <code>&lt;VM-IP&gt;</code> for this box and <code>546XX</code> for your node:
          </span>
          <pre className="node-hint-config"><code>{stanza}</code></pre>
        </li>
        <li>
          <span className="step-label">
            Register it under the <em>existing</em> <code>[nodes]</code> stanza in{" "}
            <code>rpt.conf</code>:
          </span>
          <pre className="node-hint-config"><code>{"1999 = radio@127.0.0.1:4569/1999,NONE"}</code></pre>
        </li>
      </ol>
      <p className="muted">
        Restart Asterisk (<code>sudo astres.sh</code>), then key your repeater — a
        card appears within a few seconds of unkey. Full walkthrough: the project
        README → &ldquo;AllStar node setup&rdquo;. No radio? Test with{" "}
        <code>tools/send_wav.py</code>.
      </p>
    </div>
  );
}

function Footer() {
  const { status } = useApp();
  return (
    <footer id="site-footer">
      {status?.footer_text ? <span className="footer-text">{status.footer_text}</span> : null}
      {status?.logo_ts ? (
        // eslint-disable-next-line @next/next/no-img-element
        <img className="footer-logo" src={api.logoUrl(status.logo_ts)} alt="Site logo" />
      ) : null}
    </footer>
  );
}

// Permalink scroll + flash, run entirely outside React so a ?tx= page load's
// repeated remounts can't tear it down mid-flight. Guarded so it starts once
// per target: it polls for the card to render (a page-back or two may be
// needed), then centers it and flashes it via an INLINE style on the card.
// Not a <head> style tag — Next's App Router reconciles <head> during the
// hydration churn window and garbage-collects unknown tags; and not a React
// class — component state loses the race across remounts. An inline style,
// re-asserted a few times to heal any churn wipe, is the one thing that
// reliably sticks.
function startPermalinkFlash(id: number): void {
  if (typeof window === "undefined") return;
  const w = window as unknown as { __plFlash?: number };
  if (w.__plFlash === id) return;
  w.__plFlash = id;
  const sel = `.card[data-id="${id}"]`;
  const ANIM = "watch-flash 1s ease-in-out 4";
  let tries = 0;
  const find = setInterval(() => {
    tries += 1;
    if (document.querySelector<HTMLElement>(sel)) {
      clearInterval(find);
      // A ?tx= load remounts the feed repeatedly for a couple of seconds,
      // each time REPLACING the card's DOM node with a fresh (unstyled) one —
      // so a one-shot scroll/flash gets thrown away by a later remount. Keep
      // re-applying on a short interval: center for the first stretch (to beat
      // the remounts), flash for the whole window, then clean up.
      const start = performance.now();
      const keep = setInterval(() => {
        const el = document.querySelector<HTMLElement>(sel);
        const t = performance.now() - start;
        if (el) {
          if (!el.style.animation) el.style.animation = ANIM;
          if (t < 1500) el.scrollIntoView({ block: "center" });
        }
        if (t > 5000) {
          clearInterval(keep);
          const done = document.querySelector<HTMLElement>(sel);
          if (done) done.style.animation = "";
        }
      }, 200);
    } else if (tries > 60) {
      clearInterval(find); // ~15s ceiling — target isn't going to appear
    }
  }, 250);
}

export function Feed() {
  const {
    status,
    filters,
    searching,
    searchText,
    isFiltered,
    isAdmin,
    rxActive,
    paused,
    soundOn,
    bandscopeOn,
    homeNonce,
    applyFilters,
    refreshStatus,
    reloadMdcUnits,
    setSpeakerLabel,
    setRxActive,
    setStormActive,
    setConnections,
    setWsConnected,
    openModal,
  } = useApp();

  const [rows, setRows] = useState<Transmission[]>([]);
  const [exhausted, setExhausted] = useState(false);
  // admin bulk-delete selection
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const selectAnchor = useRef<number | null>(null);
  const toggleSelect = useCallback((id: number, shiftKey = false) => {
    setSelected((prev) => {
      const n = new Set(prev);
      // Shift-click: select the contiguous range between the last-clicked card
      // (the anchor) and this one, in feed order — like a file manager.
      if (shiftKey && selectAnchor.current != null && selectAnchor.current !== id) {
        const ids = rowsRef.current.map((r) => r.id);
        const a = ids.indexOf(selectAnchor.current);
        const b = ids.indexOf(id);
        if (a !== -1 && b !== -1) {
          const [lo, hi] = a < b ? [a, b] : [b, a];
          for (let i = lo; i <= hi; i++) n.add(ids[i]);
          selectAnchor.current = id;
          return n;
        }
      }
      if (n.has(id)) n.delete(id);
      else n.add(id);
      selectAnchor.current = id;   // this click becomes the next range anchor
      return n;
    });
  }, []);
  const wrapRef = useRef<HTMLElement>(null);
  const oldestId = useRef<number | null>(null);
  const rowsRef = useRef<Transmission[]>([]);
  rowsRef.current = rows;
  const queue = useRef<WsEvent[]>([]);
  const pausedRef = useRef(paused);
  pausedRef.current = paused;
  const isFilteredRef = useRef(isFiltered);
  isFilteredRef.current = isFiltered;
  const soundRef = useRef(soundOn);
  soundRef.current = soundOn;

  // live DTMF keypad overlay: digits heard this burst + the key to glow
  // (nonce restarts the glow animation on a repeated digit)
  const [dtmfLive, setDtmfLive] = useState<{ digits: string; flash: string; nonce: number } | null>(
    null,
  );
  const dtmfTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  // streaming live captions (full state per event; ephemeral) — drives the
  // live in-progress card at the bottom of the feed
  const [caption, setCaption] = useState<{ text: string; pending: string } | null>(null);
  const captionTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  // the latest live text, read synchronously when the real card arrives
  const liveTextRef = useRef("");
  // live words carried into the real card so they don't blink to a shimmer
  // while the full transcription finishes (one over at a time, so one entry)
  const [provisional, setProvisional] = useState<{ id: number; text: string } | null>(null);

  // permalink (?tx=N): land on a specific recording. We do NOT filter — an
  // `until` filter hid every newer recording and killed live updates. The
  // target's timestamp is fetched once; a controller pages back through
  // history until the row is loaded, then the card centers + flashes itself.
  const [permalinkId, setPermalinkId] = useState<number | null>(() =>
    typeof window === "undefined"
      ? null
      : Number(new URLSearchParams(window.location.search).get("tx")) || null,
  );
  const [permalinkAt, setPermalinkAt] = useState<number | null>(null);
  const [permalinkDone, setPermalinkDone] = useState(false);
  // read by loadFeed to suppress its one-time scroll-to-bottom while we're
  // still hunting for the target (otherwise it fights the card's centering)
  const permalinkPendingRef = useRef(false);
  permalinkPendingRef.current = permalinkId != null && !permalinkDone;

  const atBottom = () => {
    const el = wrapRef.current;
    if (!el) return true;
    return el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  };
  const scrollToBottom = () => {
    const el = wrapRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  };

  const olderInFlight = useRef(false);
  const loadFeed = useCallback(
    async (older = false, limit = 50) => {
      // one older-fetch at a time: the debounced top-up and the "Load older"
      // button would otherwise race on the same before_id and prepend
      // duplicate rows / fight over the exhausted flag
      if (older) {
        if (olderInFlight.current) return;
        olderInFlight.current = true;
      }
      try {
        const params: FeedParams = { limit };
        Object.assign(params, filters);
        if (older && oldestId.current) params.before_id = oldestId.current;
        if (searching && searchText) params.q = searchText;
        let data;
        try {
          data = await api.transmissions(params);
        } catch {
          return;
        }
        const fresh = data.transmissions.slice().reverse();
        if (!older) {
          setRows(fresh);
          setSelected(new Set());        // a rebuilt feed clears the selection
          oldestId.current = fresh.length ? fresh[0].id : null;
          if (!permalinkPendingRef.current) requestAnimationFrame(scrollToBottom);
        } else if (fresh.length) {
          const el = wrapRef.current;
          const keep = el ? el.scrollHeight - el.scrollTop : 0;
          setRows((prev) => {
            const have = new Set(prev.map((r) => r.id));
            return [...fresh.filter((f) => !have.has(f.id)), ...prev];
          });
          oldestId.current = fresh[0].id;
          requestAnimationFrame(() => {
            if (el) el.scrollTop = el.scrollHeight - keep;
          });
        }
        setExhausted(data.transmissions.length < limit);
      } finally {
        if (older) olderInFlight.current = false;
      }
    },
    [filters, searching, searchText],
  );

  useEffect(() => {
    loadFeed(false);
  }, [loadFeed]);

  // re-fetch when the login state actually flips (not on the first status
  // resolve): voter RSSI is embedded per-transmission and server-stripped for
  // anonymous users, so a login/logout must reload the feed for that field to
  // match the new auth — loadFeed(false) preserves the current filters/search.
  const authSeen = useRef<boolean | null>(null);
  useEffect(() => {
    if (!status) return; // wait for the first status
    if (authSeen.current === null) {
      authSeen.current = isAdmin; // adopt the initial state without reloading
      return;
    }
    if (authSeen.current !== isAdmin) {
      authSeen.current = isAdmin;
      loadFeed(false);
    }
  }, [status, isAdmin, loadFeed]);

  // clicking the brand/logo returns to the home view. clearAllFilters (fired in
  // the same goHome) reloads the latest page and scrolls to the bottom; here we
  // also drop any active permalink and re-assert the jump, so it lands on the
  // newest recording even if a ?tx= hunt was still mid-flight.
  const homeSeen = useRef(homeNonce);
  useEffect(() => {
    if (homeNonce === homeSeen.current) return;
    homeSeen.current = homeNonce;
    setPermalinkId(null);
    setPermalinkAt(null);
    setPermalinkDone(true);
    requestAnimationFrame(scrollToBottom);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [homeNonce]);

  // step 1: kick off the remount-immune DOM poller (scroll + flash) and fetch
  // the target's timestamp so the controller below knows how far to page back.
  useEffect(() => {
    if (permalinkId == null) return;
    // the poller lives OUTSIDE React (plain setInterval) because a ?tx= load
    // remounts this component repeatedly — a React effect doing the scroll/
    // flash runs only on whichever instance happens to win the race (the
    // "hit or miss"). A window-guarded plain interval runs exactly once and
    // can't be torn down by a remount.
    startPermalinkFlash(permalinkId);
    api
      .transmission(permalinkId)
      .then(({ transmission }) => setPermalinkAt(transmission.started_at))
      .catch(() => {
        // deleted recording or a bad link — drop back to the live feed
        history.replaceState(null, "", location.pathname);
        setPermalinkId(null);
        setPermalinkDone(true);
        scrollToBottom();
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [permalinkId]);

  // step 2: page back through history until the target row is loaded (it may
  // predate the first page); the poller catches it the moment it renders.
  // Bounded by `exhausted` so an old/deleted id can't loop forever.
  useEffect(() => {
    if (permalinkId == null || permalinkDone || permalinkAt == null) return;
    if (rows.some((r) => r.id === permalinkId)) {
      setPermalinkDone(true); // present — the poller handles scroll + flash
      return;
    }
    const oldestAt = rows.length ? rows[0].started_at : Infinity;
    if (!exhausted && permalinkAt < oldestAt) {
      loadFeed(true); // older than everything loaded — page further back
    } else {
      // older than all retained history (or already gone) — give up, go live
      setPermalinkDone(true);
      scrollToBottom();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rows, permalinkId, permalinkAt, permalinkDone, exhausted, loadFeed]);

  const exhaustedRef = useRef(exhausted);
  exhaustedRef.current = exhausted;
  const topUpTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  // after deletions, pull enough older rows back in to keep the feed filled —
  // debounced so a bulk delete's burst of tx_deleted events fetches once, not N times
  const scheduleTopUp = useCallback(() => {
    if (topUpTimer.current) clearTimeout(topUpTimer.current);
    topUpTimer.current = setTimeout(() => {
      topUpTimer.current = null;
      const need = 50 - rowsRef.current.length;
      if (need > 0 && !exhaustedRef.current) loadFeed(true, need);
    }, 350);
  }, [loadFeed]);
  // a pending top-up captured the previous loadFeed (and its filters) — drop
  // it when filters/search change rather than fetch into the rebuilt feed
  useEffect(() => () => {
    if (topUpTimer.current) {
      clearTimeout(topUpTimer.current);
      topUpTimer.current = null;
    }
  }, [scheduleTopUp]);

  const appendTx = useCallback((tx: Transmission) => {
    setRows((prev) => {
      if (prev.some((r) => r.id === tx.id)) return prev.map((r) => (r.id === tx.id ? tx : r));
      return [...prev, tx];
    });
    if (oldestId.current == null) oldestId.current = tx.id;
    if (atBottom()) requestAnimationFrame(scrollToBottom);
  }, []);
  const updateTx = useCallback((tx: Transmission) => {
    setRows((prev) => (prev.some((r) => r.id === tx.id) ? prev.map((r) => (r.id === tx.id ? tx : r)) : prev));
  }, []);
  // deletion collapses the card over ~220ms before the row unmounts, so the
  // feed slides closed instead of snapping when the scroll clamp kicks in
  const [removingIds, setRemovingIds] = useState<Set<number>>(new Set());
  const removalPending = useRef<Set<number>>(new Set());
  const removeTx = useCallback((id: number) => {
    if (removalPending.current.has(id)) return;
    if (window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) {
      setRows((prev) => prev.filter((r) => r.id !== id));
      return;
    }
    removalPending.current.add(id);
    setRemovingIds((prev) => new Set(prev).add(id));
    setTimeout(() => {
      removalPending.current.delete(id);
      setRows((prev) => prev.filter((r) => r.id !== id));
      setRemovingIds((prev) => {
        const n = new Set(prev);
        n.delete(id);
        return n;
      });
    }, 260);
  }, []);

  const handleEvent = useCallback(
    (msg: WsEvent) => {
      switch (msg.type) {
        case "storm": {
          setStormActive(!!(msg as Extract<WsEvent, { type: "storm" }>).active);
          break;
        }
        case "rx": {
          const active = !!(msg as Extract<WsEvent, { type: "rx" }>).active;
          setRxActive(active);
          if (active && atBottom()) requestAnimationFrame(scrollToBottom);
          if (captionTimer.current) clearTimeout(captionTimer.current);
          if (active) {
            // a new over starts with a clean live card
            liveTextRef.current = "";
            setCaption(null);
          } else {
            // let the last words linger on the live card until the real card
            // arrives (or briefly, if a dropped tx_new never comes)
            captionTimer.current = setTimeout(() => setCaption(null), 2500);
          }
          break;
        }
        case "dtmf": {
          const d = String((msg as Extract<WsEvent, { type: "dtmf" }>).digit || "");
          if (!d) break;
          setDtmfLive((prev) => ({
            digits: ((prev?.digits || "") + d).slice(-24),
            flash: d,
            nonce: (prev?.nonce || 0) + 1,
          }));
          if (dtmfTimer.current) clearTimeout(dtmfTimer.current);
          dtmfTimer.current = setTimeout(() => setDtmfLive(null), 6000);
          break;
        }
        case "presence":
          setConnections((msg as Extract<WsEvent, { type: "presence" }>).count);
          break;
        case "bscope":
          // one FFT column ~30/sec — hand straight to the canvas, never state
          pushBscope((msg as Extract<WsEvent, { type: "bscope" }>).c);
          break;
        case "caption": {
          const m = msg as Extract<WsEvent, { type: "caption" }>;
          if (captionTimer.current) clearTimeout(captionTimer.current);
          setCaption({ text: m.text || "", pending: m.pending || "" });
          liveTextRef.current = [m.text, m.pending].filter(Boolean).join(" ").trim();
          // keep the growing live card visible if we're pinned to the edge
          if (atBottom()) requestAnimationFrame(scrollToBottom);
          // fallback self-expiry: if the socket drops mid-transmission the
          // rx:false that normally clears the card never arrives — don't
          // let a stale live card sit on screen for hours
          captionTimer.current = setTimeout(() => setCaption(null), 15000);
          break;
        }
        case "tx_new": {
          const rec = (msg as Extract<WsEvent, { type: "tx_new" }>).tx;
          // carry the live words into the real card so they persist through
          // transcription instead of flashing to a shimmer
          if (liveTextRef.current && !isFilteredRef.current) {
            setProvisional({ id: rec.id, text: liveTextRef.current });
          }
          liveTextRef.current = "";
          if (captionTimer.current) clearTimeout(captionTimer.current);
          setCaption(null); // the real card now represents this over
          if (soundRef.current) playNewRecording();
          if (!isFilteredRef.current) appendTx(rec);
          break;
        }
        case "tx_update": {
          const tx = (msg as Extract<WsEvent, { type: "tx_update" }>).tx;
          // once the over leaves "processing" the card renders the real
          // transcript — drop the live stand-in text for it
          if (tx.status !== "processing") {
            setProvisional((p) => (p && p.id === tx.id ? null : p));
          }
          // "transcription finished" = text has just appeared on an over that
          // didn't have any before (not every tx_update, which also fires for
          // MDC/origin/status changes)
          const cur = rowsRef.current;
          const prev = cur.find((r) => r.id === tx.id);
          if (soundRef.current && prev && !prev.transcript && !!tx.transcript) {
            playTranscription();
          }
          if (prev) {
            updateTx(tx); // in the feed — patch it in place
          } else if (
            !isFilteredRef.current &&
            tx.id > (cur.length ? cur[cur.length - 1].id : 0)
          ) {
            // genuinely newer than everything loaded (a tx_new we missed) —
            // safe to append at the live edge
            appendTx(tx);
          }
          // else: an update to an off-screen HISTORICAL row — from reprocess,
          // late MDC, or the retroactive revisit sweep. Dropping it is right:
          // appending would splice an old recording into the live feed out of
          // order (the "why is everything out of order" bug). It's saved in
          // the DB and shows correctly whenever that range is next loaded.
          break;
        }
        case "tx_deleted":
          removeTx((msg as Extract<WsEvent, { type: "tx_deleted" }>).id);
          scheduleTopUp();
          break;
        case "feed_reload":
          if (!isFilteredRef.current) loadFeed(false);
          break;
        case "watch_hit":
          handleWatchHit(msg as Extract<WsEvent, { type: "watch_hit" }>, soundRef.current);
          break;
        case "speaker_renamed": {
          const m = msg as Extract<WsEvent, { type: "speaker_renamed" }>;
          setSpeakerLabel(m.speaker_id, m.label);
          break;
        }
        case "mdc_units_changed":
          reloadMdcUnits().then(() => {
            if (!isFilteredRef.current) loadFeed(false);
          });
          break;
        case "model_changed":
        case "theme_changed":
        case "footer_changed":
        case "subline_changed":
        case "branding_changed":
          refreshStatus();
          break;
      }
    },
    [appendTx, updateTx, removeTx, loadFeed, scheduleTopUp, refreshStatus, reloadMdcUnits, setSpeakerLabel, setRxActive, setStormActive, setConnections],
  );

  useWebSocket((msg) => {
    if (pausedRef.current && (msg.type === "tx_new" || msg.type === "tx_update")) {
      queue.current.push(msg);
      return;
    }
    handleEvent(msg);
  }, refreshStatus, setWsConnected);

  useEffect(() => {
    if (!paused && queue.current.length) {
      const q = queue.current.splice(0);
      const wasSound = soundRef.current;
      soundRef.current = false; // don't machine-gun alerts when unpausing a backlog
      try {
        for (const m of q) handleEvent(m);
      } finally {
        soundRef.current = wasSound;
      }
    }
  }, [paused, handleEvent]);

  // keyboard shortcuts
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null;
      if (
        t &&
        (t.tagName === "INPUT" ||
          t.tagName === "TEXTAREA" ||
          t.tagName === "SELECT" ||
          t.isContentEditable)
      )
        return;
      if (e.code === "Space") {
        e.preventDefault(); // Space = play/pause (or start the newest over)
        if (!toggleActiveAudio()) {
          const rows = rowsRef.current;
          for (let i = rows.length - 1; i >= 0; i--) {
            if (rows[i].has_audio) {
              requestPlay(rows[i].id);
              break;
            }
          }
        }
      } else if (e.key === "/") {
        e.preventDefault(); // / = jump to transcript search
        document.getElementById("search")?.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
    };
  }, []);

  const items: ReactNode[] = [];
  let prev: Transmission | null = null;
  for (const tx of rows) {
    if (!prev || dayKey(prev.started_at) !== dayKey(tx.started_at)) {
      items.push(<DayDivider key={"d" + tx.id} epoch={tx.started_at} onPurged={() => loadFeed(false)} />);
    }
    items.push(
      <TransmissionCard
        key={tx.id}
        tx={tx}
        onDeleted={removeTx}
        selectable={isAdmin}
        selected={selected.has(tx.id)}
        onToggleSelect={toggleSelect}
        removing={removingIds.has(tx.id)}
        provisional={provisional?.id === tx.id ? provisional.text : undefined}
      />,
    );
    prev = tx;
  }

  const bulkDelete = () => {
    const ids = [...selected];
    if (!ids.length) return;
    openModal({
      kind: "confirm",
      title: "Delete selected?",
      message: `Delete ${ids.length} selected recording${
        ids.length === 1 ? "" : "s"
      }? This can't be undone.`,
      confirmLabel: "Delete",
      danger: true,
      onConfirm: async () => {
        const wasAtBottom = atBottom();
        let failed = 0;
        for (const id of ids) {
          try {
            await api.deleteTransmission(id);
            removeTx(id);
          } catch {
            failed++;
          }
        }
        setSelected(new Set());
        scheduleTopUp(); // usually redundant with the tx_deleted echoes, but covers a dropped WS
        // removing a batch shrinks the feed: the card-collapse animation (260ms)
        // and the top-up refill (350ms + prepend) both resize it, and the
        // browser clamps scrollTop and yanks the view upward. If we were at the
        // live edge, re-assert the bottom across that settle window.
        if (wasAtBottom) {
          [300, 550, 800].forEach((ms) =>
            setTimeout(() => requestAnimationFrame(scrollToBottom), ms));
        }
        if (failed) alert(`${failed} deletion${failed === 1 ? "" : "s"} failed.`);
      },
    });
  };

  const jumpDate = filters.until ? epochToDateInput(Number(filters.until) - 1) : "";

  return (
    <main id="feed-wrap" ref={wrapRef}>
      {bandscopeOn && status?.has_bandscope && <Bandscope />}
      <div id="feed">
        <FilterBar />
        <div className="feed-tools">
          {!exhausted && (
            <button className="text-btn" onClick={() => loadFeed(true)}>
              Load older transmissions
            </button>
          )}
          <label className="date-jump" title="Jump to a specific date in the feed">
            <FontAwesomeIcon icon={ICONS.calendar} />
            <input
              type="date"
              value={jumpDate}
              onChange={(e) =>
                applyFilters({
                  until: e.target.value ? dateToEpoch(e.target.value) + 86400 : null,
                })
              }
            />
          </label>
        </div>
        {isAdmin && status && !status.last_frame && rows.length === 0 && (
          <NodeSetupHint port={status.usrp_port} />
        )}
        {exhausted && rows.length > 0 && (
          <div className="edge-marker">
            <span>beginning of records</span>
          </div>
        )}
        {rows.length === 0 && !searching && (
          <div className="empty">
            <BrandMark className="" />
            <p>No transmissions yet</p>
            <p className="muted">Listening for audio from the node…</p>
          </div>
        )}
        {items}
        {(rxActive || (caption && (caption.text || caption.pending))) && (
          <LiveCard
            text={caption?.text || ""}
            pending={caption?.pending || ""}
            rxActive={rxActive}
          />
        )}
      </div>
      <Footer />
      {dtmfLive && (
        <DtmfOverlay digits={dtmfLive.digits} flash={dtmfLive.flash} nonce={dtmfLive.nonce} />
      )}
      {isAdmin && selected.size > 0 && (
        <div className="bulk-bar">
          <span className="bulk-count">{selected.size} selected</span>
          <button className="text-btn danger" onClick={bulkDelete}>
            Delete selected
          </button>
          <button className="text-btn" onClick={() => setSelected(new Set())}>
            Clear
          </button>
        </div>
      )}
    </main>
  );
}
