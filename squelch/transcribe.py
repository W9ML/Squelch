"""Speech-to-text via faster-whisper (optional dependency).

Model switching is non-blocking. The transcription worker is a single
thread, so it must never block on a model download — over a slow or
flaky link a multi-GB download can stall for minutes and wedge the
whole pipeline (every transmission stuck "processing"). So:

- the very first model is loaded synchronously once (normally the
  locally-present default, which is fast);
- switching to a different model loads it on a background thread while
  transcription keeps using the currently-loaded model, swapping only
  once the new one is actually ready. A stalled download therefore just
  means the switch hasn't taken effect yet — nothing backs up.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from collections import Counter

import numpy as np

log = logging.getLogger(__name__)

# Bound each HF download request so a stalled connection fails instead
# of hanging forever (set before faster-whisper/huggingface_hub import).
os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "30")

try:
    from faster_whisper import WhisperModel
    AVAILABLE = True
except ImportError:
    WhisperModel = None
    AVAILABLE = False

# The ICAO/NATO phonetic alphabet, fed to whisper as `hotwords` so it
# reliably hears spelled-out callsigns ("Whiskey Three Foxtrot...")
# instead of mangling them ("23 Foxtrot..."). Biasing beats a prompt
# here — it doesn't get echoed back as a hallucination. Safe because
# the pipeline never sends non-speech audio to transcription (the
# speech-activity gate skips kerchunks), so hotwords can't be
# hallucinated onto silence.
_NATO = ("Alpha Bravo Charlie Delta Echo Foxtrot Golf Hotel India Juliet "
         "Kilo Lima Mike November Oscar Papa Quebec Romeo Sierra Tango "
         "Uniform Victor Whiskey Xray Yankee Zulu")
_PHON_SET = frozenset(w.lower() for w in _NATO.split())

# Whisper's training-data ghosts: phrases it conjures from noise because
# they ended a million YouTube videos. Matched per-SEGMENT (normalized),
# because they also get appended after real speech on a noisy squelch
# tail. Nobody on a repeater says these — dropping them is free accuracy.
_GHOST_EXACT = {
    "thank you for watching",
    "thanks for watching",
    "thank you so much for watching",
    "thank you for watching and see you in the next video",
    "thank you for watching until the end",
    "see you in the next video",
    "please subscribe",
    "please like and subscribe",
    "dont forget to like and subscribe",
    "like comment and subscribe",
    "this video is a work of fiction any resemblance to actual persons"
    " living or dead is coincidental",
    "you",
}
_GHOST_PREFIXES = (
    "subtitles by",
    "subs by",
    "captions by",
    "captioning by",
    "transcription by",
    "translated by",
    "copyright ",
)
# Substrings that never occur in real repeater traffic — if Whisper emits
# one anywhere in a segment, the whole segment is a training-data ghost no
# matter what got glued onto it. The NASA/JPL/Caltech tag trails countless
# clips and comes through at many lengths ("NASA Jet Propulsion Laboratory",
# "Jet Propulsion Laboratory, California Institute of Technology", …), so an
# exact whole-segment match missed the short forms — catch it as a substring.
_GHOST_CONTAINS = (
    "jet propulsion laboratory",
    "united states of america",
    "subscribe",
    "like button",
)


def _is_ghost(text: str) -> bool:
    norm = " ".join(re.findall(r"[a-z0-9]+", text.lower()))
    if not norm:
        return False
    if norm in _GHOST_EXACT or norm.startswith(_GHOST_PREFIXES):
        return True
    return any(g in norm for g in _GHOST_CONTAINS)


# Whisper, biased by the callsign hotwords, will "recite" the NATO alphabet
# over a garbled or quiet tail (a fluttering mobile that stays keyed, say): a
# long run of phonetic words, often sequential, whose word timestamps stretch a
# single token across many seconds -- durations no real speech has. A genuine
# spoken callsign is short (<=6 phonetic words) and normally paced, so it sails
# through both tests. Word durations come from word_timestamps (may be empty).
def _is_hallucinated_segment(text: str, word_durs) -> bool:
    if any(d > 2.5 for d in word_durs):
        return True                            # a token stretched past speech
    toks = re.findall(r"[a-z']+", text.lower())
    if len(toks) >= 8:
        phon = sum(1 for t in toks if t in _PHON_SET)
        if phon / len(toks) >= 0.7:
            return True                        # alphabet recitation, not an ID
    return False


class Transcriber:
    def __init__(self, device: str, compute_type: str, language: str,
                 download_root):
        self.device = device
        self.compute_type = compute_type
        self.language = language or None
        self.download_root = str(download_root)
        self._model = None
        self._model_name: str | None = None
        self._loading: str | None = None      # model being loaded in bg
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        return AVAILABLE

    @property
    def loaded_model(self) -> str | None:
        return self._model_name

    @property
    def loading_model(self) -> str | None:
        """Name of a model currently downloading/loading in the
        background, or None."""
        return self._loading

    def _load(self, name: str):
        log.info("loading whisper model %s (%s/%s)",
                 name, self.device, self.compute_type)
        return WhisperModel(name, device=self.device,
                            compute_type=self.compute_type,
                            download_root=self.download_root)

    def ensure_model(self, name: str) -> bool:
        """Request `name` as the active model without blocking. Returns
        True if it's already the active model, False if a switch was
        started (or is already in progress)."""
        if not AVAILABLE:
            return False
        with self._lock:
            if self._model_name == name:
                return True
            if self._loading == name:
                return False
            self._loading = name
        threading.Thread(target=self._bg_load, args=(name,),
                         daemon=True).start()
        return False

    def _bg_load(self, name: str) -> None:
        try:
            model = self._load(name)
            with self._lock:
                self._model = model
                self._model_name = name
            log.info("whisper model %s is ready", name)
        except Exception:
            # e.g. a stalled/failed download; keep serving the old model
            log.exception("failed to load whisper model %s (keeping %s)",
                          name, self._model_name)
        finally:
            with self._lock:
                if self._loading == name:
                    self._loading = None

    def transcribe(self, audio_16k: np.ndarray, model_name: str,
                   initial_prompt: str | None = None,
                   beam_size: int = 5, use_hotwords: bool = True) -> tuple:
        """audio_16k: float32 mono at 16 kHz in [-1, 1]. Runs in a worker
        thread and NEVER blocks on a model load — model loading (incl.
        the first one and any switch) always happens on a background
        thread. If no model is ready yet, transcription is skipped for
        this transmission (it still gets saved and MDC-decoded) and
        resumes automatically once a model finishes loading.

        Anti-hallucination is layered: the pipeline skips non-speech
        audio before it ever reaches here, and low-confidence /
        repetitive / prompt-echo segments are dropped below. (VAD
        filtering is intentionally NOT used — it clips leading
        phonetics.)

        Returns (text, words) where words is [[word, start_s, end_s], ...]
        for karaoke sync. text is None if no model is loaded yet (retry
        later), "" if genuinely no speech."""
        if not AVAILABLE:
            return "", []
        self.ensure_model(model_name)          # non-blocking
        with self._lock:
            model = self._model                # current model, or None
        if model is None:
            # nothing loaded yet (cold start / mid-switch): caller
            # should retry once a model is ready — None, not "", so the
            # skip is distinguishable from genuinely empty speech
            return None, []
        # VAD filtering is deliberately OFF: Silero clips the low-energy
        # onset of leading phonetics (turns "Whiskey 3" into "23"). The
        # pipeline's own speech-activity gate handles non-speech, and
        # the segment guards below catch hallucinated output.
        segments, _info = model.transcribe(
            audio_16k, language=self.language, beam_size=beam_size,
            condition_on_previous_text=False,
            initial_prompt=initial_prompt or None,
            # hotwords bias callsign phonetics but are startlingly expensive
            # on CPU (a 26-word bias turns a ~1.2s base.en decode into 6-9s),
            # so the live captioner runs without them — the final GPU
            # large-v3 pass restores callsign accuracy on the card
            hotwords=_NATO if use_hotwords else None,
            vad_filter=False,
            word_timestamps=True,
            # suppress text invented across a silent gap longer than this —
            # exactly how a hallucinated tail detaches from real speech
            hallucination_silence_threshold=2.0,
            no_speech_threshold=0.5)
        texts, words = [], []
        for seg in segments:
            nsp = getattr(seg, "no_speech_prob", 0.0) or 0.0
            alp = getattr(seg, "avg_logprob", 0.0) or 0.0
            cr = getattr(seg, "compression_ratio", 1.0) or 1.0
            segw = getattr(seg, "words", None) or []
            if nsp > 0.6 and alp < -1.0:
                continue                       # confident it's not speech
            if cr > 2.4:
                continue                       # repetition-loop artifact
            if _is_ghost(seg.text):
                continue                       # training-data ghost phrase
            if _is_hallucinated_segment(seg.text,
                                        [w.end - w.start for w in segw]):
                continue                       # phonetic recitation / a token
                                               # stretched over a garbled tail
            texts.append(seg.text.strip())
            for w in segw:
                words.append([w.word.strip(), round(w.start, 2),
                              round(w.end, 2)])
        text = " ".join(texts).strip()
        if self._looks_like_loop(text):
            # a word repeated over and over — a hallucination loop that
            # the per-segment compression check missed (e.g. a prompt
            # echoing "N9PRK, N9PRK, N9PRK...")
            return "", []
        if self._impossible_density(text, len(audio_16k) / 16000.0):
            # more words than a human can say in the clip — large-v3
            # conjuring training-data ghosts ("This video is a work of
            # fiction...") out of a sub-second squelch crash
            return "", []
        return text, words

    @staticmethod
    def _impossible_density(text: str, duration_s: float) -> bool:
        n_words = len(re.findall(r"[A-Za-z0-9']+", text))
        # fast speech tops out near 5 words/s; +2 slack keeps legitimate
        # clipped overs ("KD9NSC clear") on sub-second clips
        return n_words > duration_s * 5.0 + 2

    @staticmethod
    def _looks_like_loop(text: str) -> bool:
        words = [w for w in re.findall(r"[A-Za-z0-9']+", text.lower())
                 if len(w) > 1]
        if len(words) < 10:
            return False
        common, count = Counter(words).most_common(1)[0]
        return count >= 6 and count / len(words) > 0.25
