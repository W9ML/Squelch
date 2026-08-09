"""On-demand cloud transcription via ElevenLabs Scribe.

This is the "second opinion" path only: the default pipeline stays fully local
(faster-whisper on the box). ElevenLabs is used ONLY when an admin explicitly
reprocesses one specific over through it, so at most that single over's audio
ever leaves the machine -- never the live stream. Keeps the offline-first,
privacy-first posture intact while giving a different model a shot at the overs
whisper botches (marginal audio, or the whisper-specific hotword-echo
hallucination, which a non-whisper model has no bias to reproduce).

The API key is a live credential and is read from the ELEVENLABS_API_KEY
environment variable -- never the config file.
"""
from __future__ import annotations

import logging
import os

import requests

log = logging.getLogger(__name__)

_URL = "https://api.elevenlabs.io/v1/speech-to-text"
_KEY_ENV = "ELEVENLABS_API_KEY"


class ElevenLabsError(Exception):
    """Any failure talking to ElevenLabs; message is user-facing."""


def available() -> bool:
    """True when an API key is present (the feature also needs cfg.enabled)."""
    return bool(os.environ.get(_KEY_ENV))


def transcribe_file(path: str, model_id: str = "scribe_v2",
                    language_code: str | None = "eng",
                    timeout: float = 120.0) -> tuple[str, list]:
    """POST a stored audio file to ElevenLabs Scribe and return
    (text, words) in Squelch's own shape: words is [[word, start, end], ...],
    keeping only real word tokens (spacing / audio-event entries dropped).

    Runs synchronously (call it via asyncio.to_thread). Raises ElevenLabsError
    on any failure so the caller can surface a message and leave the existing
    transcript untouched.
    """
    key = os.environ.get(_KEY_ENV)
    if not key:
        raise ElevenLabsError(f"{_KEY_ENV} is not set")
    data = {"model_id": model_id, "timestamps_granularity": "word"}
    if language_code:
        data["language_code"] = language_code
    try:
        with open(path, "rb") as fh:
            resp = requests.post(
                _URL, headers={"xi-api-key": key},
                data=data, files={"file": fh}, timeout=timeout)
    except OSError as e:
        raise ElevenLabsError(f"cannot read audio: {e}") from e
    except requests.RequestException as e:
        raise ElevenLabsError(f"request failed: {e}") from e
    if resp.status_code != 200:
        detail = resp.text[:200].strip().replace("\n", " ")
        raise ElevenLabsError(f"HTTP {resp.status_code}: {detail}")
    try:
        body = resp.json()
    except ValueError as e:
        raise ElevenLabsError(f"bad JSON response: {e}") from e

    text = (body.get("text") or "").strip()
    words: list = []
    for w in body.get("words") or []:
        if w.get("type") != "word":
            continue                       # skip "spacing" / "audio_event"
        tok = (w.get("text") or "").strip()
        if not tok:
            continue
        words.append([tok, round(float(w.get("start", 0.0)), 2),
                      round(float(w.get("end", 0.0)), 2)])
    return text, words
