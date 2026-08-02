"""Voter (RTCM) status capture for voted repeater systems.

Subscribes to one or more Allmon2-style voterserver.php SSE streams
(adapted from W9ML's voter_rssi_logger.py) and keeps a rolling buffer
of per-receiver RSSI snapshots. When a transmission completes, the
samples spanning it are stored with the recording so the web UI can
replay which receiver site was voted, synced to audio playback.

Stream events look like:

    event: voter
    data: {"node":"46655","info":"N9IAA 146.6850MHz ...","clients":
           [{"name":"Crown_Point","rssi":"230","barcolor":"greenyellow",...}]}

barcolor greenyellow = the voted station, blue = voting, cyan = mix.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections import deque

from .voter_crypto import decrypt_message

log = logging.getLogger(__name__)

BUFFER_SECS = 900          # rolling history kept per node
RECONNECT_SECS = 5.0
# a link report older than this means the Pi's source monitor is presumed
# down; when gating, we fail OPEN (keep polling) rather than silently kill
# voter capture on a dead reporter
LINK_TTL = 120.0
# how often a paused source re-checks link state — kept short so a source
# resumes within ~1s of the node linking (a cheap in-memory check, no network)
GATE_POLL_SECS = 1.0

_NODE_RE = re.compile(r"/nodes/(\d+)\b|[?&]node=(\d+)")


def node_from_url(url: str) -> str | None:
    """The AllStar node a voter source URL is for, parsed from the common
    forms (…/api/nodes/46655/voter or …voterserver.php?node=46655). None
    when it can't be determined (then that source is never link-gated)."""
    m = _NODE_RE.search(url)
    return (m.group(1) or m.group(2)) if m else None


class LinkState:
    """Thread-safe view of which AllStar nodes the monitored node is
    currently linked to, reported periodically by the Pi's source monitor
    (tools/source_monitor.py -> POST /api/link). Used to gate voter
    polling: if we're not linked to a node, there's no traffic to tie its
    voter data to, so don't poll that node's voter stream."""

    def __init__(self):
        self._connected: set[str] = set()
        self._reported_at = 0.0
        self._lock = threading.Lock()

    def report(self, nodes, ts: float | None = None) -> None:
        ts = ts if ts is not None else time.time()
        with self._lock:
            self._connected = {str(n).strip() for n in nodes if str(n).strip()}
            self._reported_at = ts

    def status(self, node: str, now: float | None = None,
               ttl: float = LINK_TTL) -> str:
        """'connected' | 'disconnected' | 'stale' (no fresh report)."""
        now = now if now is not None else time.time()
        with self._lock:
            if now - self._reported_at > ttl:
                return "stale"
            return "connected" if str(node) in self._connected else "disconnected"

    def snapshot(self) -> tuple[list[str], float]:
        with self._lock:
            return sorted(self._connected), self._reported_at


def parse_voter_event(payload: str) -> dict | None:
    """One SSE `data:` JSON payload -> normalized snapshot, or None."""
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        return None
    clients = data.get("clients")
    if not isinstance(clients, list) or "node" not in data:
        return None
    out = []
    for c in clients:
        if not isinstance(c, dict) or "rssi" not in c:
            continue
        try:
            rssi = int(c.get("rssi") or 0)
        except (ValueError, TypeError):
            rssi = 0
        # new encrypted stream: {"client","rssi":int,"isVoted":bool}
        # old voterserver.php:  {"name","rssi":str,"barcolor":"greenyellow"}
        name = c.get("name") or c.get("client") or "unknown"
        if "isVoted" in c:
            voted = bool(c.get("isVoted"))
        else:
            voted = c.get("barcolor") == "greenyellow"
        out.append({"name": str(name).strip(), "rssi": rssi, "voted": voted})
    return {"node": str(data["node"]), "info": str(data.get("info") or ""),
            "clients": out}


class VoterCollector:
    """Background threads (one per source URL) feeding a rolling,
    thread-safe buffer of snapshots keyed by node number."""

    def __init__(self, sources: list[str], min_interval: float = 0.1,
                 key: bytes | None = None, gate_on_connect: bool = False,
                 idle_timeout: bool = False, idle_minutes: float = 10.0,
                 disabled: bool = False):
        self.sources = sources
        # 0 = keep every event the stream sends (the stream's own emit
        # rate is the effective ceiling, e.g. ~6.6 Hz for Allmon2)
        self.min_interval = max(0.02, min_interval)
        # 16-byte AES key for encrypted streams, or None for plaintext
        self.key = key
        # only poll a source's voter stream while its node is linked (per the
        # Pi's link reports); off = always poll (the original behavior)
        self.gate_on_connect = gate_on_connect
        # idle timeout (runtime-toggleable from the admin UI): also pause
        # polling after this long with no USRP traffic, resuming on the next
        # transmission. The threshold doubles as the post-activity linger, so
        # gaps within a QSO never drop the stream.
        self._idle_enabled = idle_timeout
        self._idle_secs = max(1.0, idle_minutes * 60)
        self._activity_probe = None          # () -> last USRP frame walltime
        # master kill switch (runtime): fully quiet all polling regardless of
        # link/idle state
        self._disabled = disabled
        self.link = LinkState()
        self._node_of = {url: node_from_url(url) for url in sources}
        self._buf: dict[str, deque] = {}
        self._info: dict[str, str] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    # ---- lifecycle ----

    def start(self) -> None:
        for url in self.sources:
            t = threading.Thread(target=self._run, args=(url,), daemon=True,
                                 name=f"voter:{url[-24:]}")
            t.start()
            self._threads.append(t)
        if self.sources:
            log.info("voter collector: %d source(s)%s", len(self.sources),
                     " (link-gated)" if self.gate_on_connect else "")

    def stop(self) -> None:
        self._stop.set()

    def report_link(self, nodes) -> None:
        """Record the current set of linked nodes (from the Pi's source
        monitor). Gating threads pick it up on their next check."""
        self.link.report(nodes)

    def set_idle_timeout(self, enabled: bool, minutes: float | None = None) -> None:
        """Toggle the idle timeout (admin UI). Threads pick it up on their
        next ~1s check."""
        self._idle_enabled = bool(enabled)
        if minutes is not None:
            self._idle_secs = max(1.0, float(minutes) * 60)

    def set_activity_probe(self, fn) -> None:
        """fn() -> walltime of the last USRP frame (channel-activity clock)."""
        self._activity_probe = fn

    def set_disabled(self, disabled: bool) -> None:
        """Master switch (admin UI): fully quiet polling when True."""
        self._disabled = bool(disabled)

    @property
    def idle_minutes(self) -> float:
        return round(self._idle_secs / 60, 3)

    @property
    def idle_enabled(self) -> bool:
        return self._idle_enabled

    @property
    def disabled(self) -> bool:
        return self._disabled

    def status_snapshot(self) -> dict:
        """Observability: gating mode, what's linked, and whether each
        source is currently polling."""
        connected, reported_at = self.link.snapshot()
        idle_for = None
        if self._activity_probe is not None:
            last = self._activity_probe() or 0.0
            if last > 0:
                idle_for = round(max(0.0, time.time() - last), 1)
        sources = []
        for u in self.sources:
            r = self._poll_reason(u)
            sources.append({"node": self._node_of.get(u), "url": u,
                            "polling": r == "polling", "reason": r})
        return {
            "gated": self.gate_on_connect,
            "disabled": self._disabled,
            "idle_timeout": self._idle_enabled,
            "idle_minutes": self.idle_minutes,
            "idle_for_secs": idle_for,
            "linked_nodes": connected,
            "link_reported_at": reported_at or None,
            "sources": sources,
            "summary": self._summarize(sources),
        }

    def _summarize(self, sources: list[dict]) -> str:
        """One-word overall state: polling | paused | partial | disabled |
        none."""
        reasons = {s["reason"] for s in sources}
        if not sources:
            return "none"
        if reasons == {"disabled"}:
            return "disabled"
        if reasons == {"polling"}:
            return "polling"
        if "polling" in reasons:
            return "partial"
        return "paused"

    def _gating(self) -> bool:
        """True when any pause condition is in effect (used to enable the
        mid-stream drop check)."""
        return self._disabled or self.gate_on_connect or self._idle_enabled

    def _poll_reason(self, url: str) -> str:
        """Why a source is / isn't polling right now:
        'polling' | 'disabled' | 'unlinked' | 'idle'."""
        # master switch: fully off
        if self._disabled:
            return "disabled"
        # link gate: pause while not linked to the source's node
        if self.gate_on_connect:
            node = self._node_of.get(url)
            if node and self.link.status(node) == "disconnected":
                return "unlinked"
            # "stale" (reporter down) -> fall through, fail open
        # idle gate: pause after a long quiet stretch, resume on next traffic
        if self._idle_enabled and self._activity_probe is not None:
            last = self._activity_probe() or 0.0
            if last <= 0 or time.time() - last > self._idle_secs:
                return "idle"
        return "polling"

    def _should_poll(self, url: str) -> bool:
        return self._poll_reason(url) == "polling"

    def _run(self, url: str) -> None:
        import requests
        backoff = RECONNECT_SECS
        paused = False
        while not self._stop.is_set():
            if not self._should_poll(url):
                if not paused:
                    log.info("voter: pausing poll of %s (disabled, node %s "
                             "not linked, or channel idle)",
                             url, self._node_of.get(url))
                    paused = True
                self._stop.wait(GATE_POLL_SECS)
                continue
            if paused:
                log.info("voter: resuming poll of %s", url)
                paused = False
            try:
                with requests.get(url, stream=True, timeout=(10, 90),
                                  headers={"Accept": "text/event-stream"}) as resp:
                    resp.raise_for_status()
                    log.info("voter stream connected: %s%s", url,
                             " (encrypted)" if self.key else "")
                    backoff = RECONNECT_SECS
                    self._consume(url, resp.iter_lines(decode_unicode=True))
            except Exception as e:
                if not self._stop.is_set():
                    log.warning("voter stream error (%s): %s — reconnecting "
                                "in %.0fs", url, e, backoff)
                    self._stop.wait(backoff)
                    # back off on repeated failures (e.g. the voter page
                    # is down) so we don't spam the logs forever
                    backoff = min(backoff * 2, 300.0)

    def _consume(self, url: str, lines) -> None:
        """Process an SSE line iterable, decrypting and storing voter
        samples. `event: voter` data is decrypted when a key is set;
        `event: error` data is plain JSON and just logged."""
        last = 0.0
        last_link_check = time.time()
        event_type = None
        for line in lines:
            if self._stop.is_set():
                return
            # close the stream promptly when the node drops or the channel
            # goes idle (checked at most every 2s so it's cheap even at the
            # stream's full event rate)
            if self._gating():
                t = time.time()
                if t - last_link_check > 2.0:
                    last_link_check = t
                    if not self._should_poll(url):
                        log.info("voter: closing stream %s (node %s dropped "
                                 "or channel idle)", url, self._node_of.get(url))
                        return
            if line == "":
                event_type = None
                continue
            if line.startswith("event:"):
                event_type = line[6:].strip()
                continue
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if event_type == "error":
                log.warning("voter stream error event (%s): %s",
                            url, payload[:200])
                continue
            if event_type == "tictoc":
                continue                         # animation heartbeat
            now = time.time()
            if now - last < self.min_interval:
                continue
            if self.key is not None and event_type == "voter":
                try:
                    payload = decrypt_message(payload, self.key)
                except Exception as e:
                    log.debug("voter decrypt failed: %s", e)
                    continue
            snap = parse_voter_event(payload)
            if snap is None or not snap["clients"]:
                continue
            last = now
            self._store(now, snap)

    def _store(self, ts: float, snap: dict) -> None:
        node = snap["node"]
        maxlen = int(BUFFER_SECS / self.min_interval)
        with self._lock:
            if node not in self._buf:
                self._buf[node] = deque(maxlen=maxlen)
            self._buf[node].append((ts, snap["clients"]))
            self._info[node] = snap["info"]

    # ---- per-transmission extraction ----

    def slice_window(self, start: float, end: float) -> dict | None:
        """Samples covering [start, end] for every node that saw signal
        during it, compacted for storage:

        {"nodes": [{"node","info","clients":[names],
                    "samples": [[t_rel, [rssi...], voted_bitmask], ...]}]}
        """
        nodes_out = []
        with self._lock:
            items = [(n, list(buf)) for n, buf in self._buf.items()]
            infos = dict(self._info)
        for node, samples in items:
            window = [(ts, cl) for ts, cl in samples
                      if start - self.min_interval <= ts <= end + self.min_interval]
            if not window:
                continue
            # stable client order from the first sample
            names = [c["name"] for c in window[0][1]]
            index = {n: i for i, n in enumerate(names)}
            rows = []
            any_signal = False
            for ts, clients in window:
                rssi = [0] * len(names)
                mask = 0
                for c in clients:
                    i = index.get(c["name"])
                    if i is None:       # receiver appeared mid-window
                        index[c["name"]] = i = len(names)
                        names.append(c["name"])
                        for r in rows:
                            r[1].append(0)
                        rssi.append(0)
                    rssi[i] = c["rssi"]
                    if c["voted"] and c["rssi"] > 0:
                        mask |= 1 << i
                if any(v > 0 for v in rssi):
                    any_signal = True
                rows.append([round(max(0.0, ts - start), 1), rssi, mask])
            if any_signal:
                nodes_out.append({"node": node, "info": infos.get(node, ""),
                                  "clients": names, "samples": rows})
        return {"nodes": nodes_out} if nodes_out else None
