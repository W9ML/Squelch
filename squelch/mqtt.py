"""MQTT publishing of finished transmissions (e.g. into Home Assistant).

Optional dependency: if ``paho-mqtt`` isn't installed the publisher degrades to
a no-op so the rest of the app runs unchanged (same pattern as webpush). Off by
default; enable with an ``[mqtt]`` section. The broker password is read from the
SQUELCH_MQTT_PASSWORD environment variable, never the TOML (it's a live
credential — same rule as ELEVENLABS_API_KEY).

Topics, under ``topic_prefix`` (default "squelch"):

  <prefix>/status        "online" / "offline" (retained; offline is the LWT)
  <prefix>/transmission  one JSON message per finished LIVE over (QoS 1)

With ``discovery`` on, Home Assistant MQTT-discovery configs are published
(retained) so a "Squelch" device appears with a connectivity sensor and a
"Last transmission" sensor carrying the full payload as attributes.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone

from .callsigns import extract_callsigns, speaker_callsign
from .qso import is_net_cue

log = logging.getLogger(__name__)

try:
    import paho.mqtt.client as paho
    _HAVE_PAHO = True
except Exception:  # pragma: no cover - depends on optional install
    _HAVE_PAHO = False

PASSWORD_ENV = "SQUELCH_MQTT_PASSWORD"
TRANSCRIPT_MAX = 400


def available() -> bool:
    """True when MQTT can actually be used (paho-mqtt importable)."""
    return _HAVE_PAHO


def primary_callsign(record: dict) -> str | None:
    """Who keyed up, as a callsign, or None. A named speaker's callsign is the
    strongest evidence; otherwise only an explicit spoken self-ID ("this is
    W9ML") counts — a bare callsign in an over is often the station being
    CALLED, not the caller."""
    if record.get("speaker_named") and record.get("speaker_label"):
        calls = extract_callsigns(record["speaker_label"])
        if calls:
            return calls[0]
    cs, strength = speaker_callsign(record.get("transcript") or "")
    return cs if strength == "strong" else None


def build_payload(record: dict, qso_size: int = 0, base_url: str = "") -> dict:
    """The JSON published for one finished over. Pure — no I/O."""
    callsign = primary_callsign(record)
    speaker = record.get("speaker_label") if record.get("speaker_named") else None
    transcript = (record.get("transcript") or "").strip()
    started = record.get("started_at")
    mdc_units = [m.get("unit_raw") or m.get("unit_id_hex")
                 for m in (record.get("mdc") or []) if isinstance(m, dict)]
    return {
        "id": record.get("id"),
        "time": (datetime.fromtimestamp(started, timezone.utc).isoformat()
                 if started else None),
        "duration_s": round((record.get("duration_ms") or 0) / 1000, 1),
        # one display string: callsign > named speaker > "Unknown"
        "who": callsign or speaker or "Unknown",
        "callsign": callsign,
        "speaker": speaker,
        "speaker_verified": record.get("speaker_verified"),
        "callsigns_heard": record.get("callsigns") or [],
        "origin": record.get("origin"),
        "mdc_units": [u for u in mdc_units if u],
        "transcript": transcript[:TRANSCRIPT_MAX],
        "qso_id": record.get("qso_id"),
        "qso_size": qso_size,
        "net_cue": is_net_cue(transcript),
        "url": (f"{base_url.rstrip('/')}/?tx={record.get('id')}"
                if base_url and record.get("id") is not None else None),
    }


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_") or "squelch"


class MqttPublisher:
    """Thin wrapper over a paho client running its own network thread.
    ``publish_transmission`` is safe to call from the asyncio loop: paho only
    queues the message; the network thread sends it and reconnects as needed."""

    def __init__(self, cfg, client_factory=None):
        self.cfg = cfg
        self.prefix = cfg.mqtt_topic_prefix.strip("/") or "squelch"
        self.status_topic = f"{self.prefix}/status"
        self.tx_topic = f"{self.prefix}/transmission"
        self.ident = _slug(cfg.callsign or cfg.node_number or self.prefix)
        self._client = None
        self._factory = client_factory

    def _make_client(self):
        if self._factory is not None:
            return self._factory()
        return paho.Client(paho.CallbackAPIVersion.VERSION2,
                           client_id=f"squelch-{self.ident}")

    def start(self) -> bool:
        if not (self.cfg.mqtt_enabled and self.cfg.mqtt_host):
            return False
        if self._factory is None and not _HAVE_PAHO:
            log.warning("mqtt enabled but paho-mqtt isn't installed — skipping")
            return False
        c = self._make_client()
        if self.cfg.mqtt_username:
            c.username_pw_set(self.cfg.mqtt_username,
                              os.environ.get(PASSWORD_ENV) or None)
        if self.cfg.mqtt_tls:
            c.tls_set()
        c.will_set(self.status_topic, "offline", qos=1, retain=True)
        c.on_connect = self._on_connect
        c.reconnect_delay_set(min_delay=2, max_delay=120)
        c.connect_async(self.cfg.mqtt_host, self.cfg.mqtt_port, keepalive=60)
        c.loop_start()
        self._client = c
        log.info("mqtt publishing to %s:%d under %s/",
                 self.cfg.mqtt_host, self.cfg.mqtt_port, self.prefix)
        return True

    def stop(self) -> None:
        c, self._client = self._client, None
        if c is None:
            return
        try:
            c.publish(self.status_topic, "offline", qos=1, retain=True)
            c.disconnect()
            c.loop_stop()
        except Exception:
            log.exception("mqtt shutdown failed")

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if getattr(reason_code, "is_failure", False) or (
                isinstance(reason_code, int) and reason_code != 0):
            log.warning("mqtt connect refused: %s", reason_code)
            return
        log.info("mqtt connected")
        if self.cfg.mqtt_discovery:
            for topic, conf in self.discovery_configs():
                client.publish(topic, json.dumps(conf), qos=1, retain=True)
        client.publish(self.status_topic, "online", qos=1, retain=True)

    def discovery_configs(self) -> list[tuple[str, dict]]:
        """Home Assistant MQTT-discovery messages for the Squelch device."""
        dp = self.cfg.mqtt_discovery_prefix.strip("/") or "homeassistant"
        name = f"Squelch {self.cfg.callsign or self.cfg.node_number}".strip()
        device = {"identifiers": [f"squelch_{self.ident}"], "name": name,
                  "manufacturer": "Squelch", "model": "AllStar monitor"}
        if self.cfg.mqtt_base_url:
            device["configuration_url"] = self.cfg.mqtt_base_url
        avail = {"availability_topic": self.status_topic}
        return [
            (f"{dp}/binary_sensor/squelch_{self.ident}/online/config", {
                "name": "Online", "unique_id": f"squelch_{self.ident}_online",
                "state_topic": self.status_topic, "payload_on": "online",
                "payload_off": "offline", "device_class": "connectivity",
                "device": device}),
            (f"{dp}/sensor/squelch_{self.ident}/last_tx/config", {
                "name": "Last transmission",
                "unique_id": f"squelch_{self.ident}_last_tx",
                "state_topic": self.tx_topic,
                "value_template": "{{ value_json.who }}",
                "json_attributes_topic": self.tx_topic,
                "icon": "mdi:radio-handheld", "device": device, **avail}),
        ]

    def publish_transmission(self, record: dict, qso_size: int = 0) -> None:
        if self._client is None:
            return
        payload = build_payload(record, qso_size, self.cfg.mqtt_base_url)
        self._client.publish(self.tx_topic, json.dumps(payload), qos=1)
