"""Extract amateur radio callsigns from Whisper transcripts.

Whisper renders a spoken callsign several ways:
    "W9ML"                        (direct)
    "W 9 M L" / "W9M L"           (spaced letters)
    "whiskey nine mike lima"      (phonetic alphabet)
    "whiskey niner mike lima"     (aviation-style niner)

All are normalized into candidate character streams and matched against
the standard callsign shape: 1-2 letter prefix, one digit, 1-3 letter
suffix. A digit is required, which keeps ordinary English words from
matching.
"""

from __future__ import annotations

import re

CALL_RE = re.compile(r"[A-Z]{1,2}[0-9][A-Z]{1,3}")

_PHONETIC = {
    "alpha": "A", "alfa": "A", "bravo": "B", "charlie": "C", "delta": "D",
    "echo": "E", "foxtrot": "F", "golf": "G", "hotel": "H", "india": "I",
    "juliet": "J", "juliett": "J", "kilo": "K", "lima": "L", "mike": "M",
    "november": "N", "oscar": "O", "papa": "P", "quebec": "Q",
    "romeo": "R", "sierra": "S", "tango": "T", "uniform": "U",
    "victor": "V", "whiskey": "W", "whisky": "W", "xray": "X",
    "x-ray": "X", "yankee": "Y", "zulu": "Z",
}
_DIGITS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
    "niner": "9",
}


# A "pair" is (token, strong). `strong` marks a token as trustworthy
# callsign material: a phonetic letter (MIKE->M) or a literal token that
# already carries a digit beside a letter (W9, KD9, KB90RJ). Spoken digit
# words (ONE->1), bare numerals, stray single letters and plain English
# words are NOT strong on their own. Provenance is what lets the merge/join
# streams tell "Kilo Delta 9 NSC" -> KD9NSC (real) apart from
# "...to one of..." -> TO1OF (ordinary speech welded into a callsign shape).
Pair = tuple[str, bool]


def _base_pairs(tokens: list[str], phonetic: bool) -> list[Pair]:
    """Tag each token with provenance. With phonetic=True, phonetic-alphabet
    and digit words are mapped to their characters (WHISKEY->W, NINE->9), so
    mixed forms like "W9 Mike Lima" get the same treatment as plain letters."""
    pairs: list[Pair] = []
    for t in tokens:
        low = t.lower()
        if phonetic and low in _PHONETIC:
            pairs.append((_PHONETIC[low], True))      # phonetic letter -> strong
        elif phonetic and low in _DIGITS:
            pairs.append((_DIGITS[low], False))        # spoken digit -> weak
        else:
            strong = (any(c.isalpha() for c in t)
                      and any(c.isdigit() for c in t))
            pairs.append((t, strong))                  # literal W9 / KD9 -> strong
    return pairs


def _merge_single_chars(pairs: list[Pair]) -> list[Pair]:
    """Collapse runs of single-character tokens: W 9 M L -> W9ML. A merged
    run is strong if any of its characters was strong, so KILO DELTA 9 -> KD9
    stays strong while stray S + spoken ONE -> S1 does not."""
    merged: list[Pair] = []
    run: list[str] = []
    run_strong = False
    for tok, strong in list(pairs) + [("", False)]:
        if len(tok) == 1 and tok.isalnum():
            run.append(tok)
            run_strong = run_strong or strong
        else:
            if run:
                merged.append(("".join(run), run_strong))
                run, run_strong = [], False
            if tok:
                merged.append((tok, strong))
    return merged


def _join_callsigns(pairs: list[Pair]) -> list[Pair]:
    """Merge adjacent tokens whose concatenation IS a callsign
    ("W9M L" -> "W9ML") — full-match only, and only when the fused span
    carries at least one strong token, so plain words plus a spoken digit
    ("TO 1 OF") don't weld into a callsign shape."""
    joined: list[Pair] = []
    i = 0
    while i < len(pairs):
        for k in (3, 2):
            if i + k <= len(pairs):
                window = pairs[i:i + k]
                blob = "".join(tok for tok, _ in window)
                if CALL_RE.fullmatch(blob) and any(s for _, s in window):
                    joined.append((blob, True))
                    i += k
                    break
        else:
            joined.append(pairs[i])
            i += 1
    return joined


# whisper often writes the letter O as a zero inside callsigns
# ("KB90RJ" for KB9ORJ); after the numeral position, 0 must be a letter
_ZERO_FIX_RE = re.compile(r"\b([A-Z]{1,2}[0-9])([A-Z0-9]{1,3})\b")


def _zero_fix(stream: str) -> str:
    return _ZERO_FIX_RE.sub(
        lambda m: m.group(1) + m.group(2).replace("0", "O"), stream)


def _candidates(text: str) -> list[str]:
    """Build normalized character streams worth regex-scanning."""
    up = re.sub(r"[^A-Z0-9\s]", " ", text.upper())
    tokens = up.split()

    streams = []
    # plain letters first, then a phonetic/digit-word-mapped pass so mixed
    # forms like "W9 Mike Lima" get the same merge/join treatment
    for phonetic in (False, True):
        pairs = _base_pairs(tokens, phonetic)
        merged = _merge_single_chars(pairs)
        streams.append(" ".join(t for t, _ in pairs))
        streams.append(" ".join(t for t, _ in merged))
        streams.append(" ".join(t for t, _ in _join_callsigns(merged)))
    streams += [_zero_fix(s) for s in list(streams)]
    return streams


def extract_callsigns(text: str) -> list[str]:
    """All callsigns found, in transcript order, de-duplicated."""
    if not text:
        return []
    seen, out = set(), []
    for stream in _candidates(text):
        for m in CALL_RE.finditer(stream):
            # avoid matching inside a longer alnum blob (e.g. serials)
            s, e = m.start(), m.end()
            if s > 0 and stream[s - 1].isalnum():
                continue
            if e < len(stream) and stream[e].isalnum():
                continue
            cs = m.group()
            if cs not in seen:
                seen.add(cs)
                out.append(cs)
    # different streams can yield truncated variants of the same call
    # ("W9M" alongside "W9ML"); keep only the fullest form
    return [c for c in out
            if not any(o != c and o.startswith(c) for o in out)]


# phrases that mean a nearby callsign is the station being *addressed*, not
# the speaker — net control says "KB9ORJ go ahead", it doesn't mean they ARE
# KB9ORJ. If any of these appear we refuse to read a bare call as self-ID.
_ADDRESS_RE = re.compile(
    r"\b(go\s*-?\s*ahead|come\s*back|you'?re\s+(up|next|on)|"
    r"your\s+(up|next)|(over|back)\s+to\s+you|calling|"
    r"stand\s*by|thank(s|\s+you)?|welcome)\b", re.IGNORECASE)


def speaker_callsign(text: str) -> tuple[str | None, str | None]:
    """Best guess at the *speaker's own* callsign, as TIERED EVIDENCE.

    A spoken callsign is evidence, never truth — hams routinely speak
    callsigns that aren't theirs ("KB9ORJ go ahead" addresses KB9ORJ;
    "KD9NSC, W9ML are you there?" is W9ML calling KD9NSC). Returns
    (callsign, strength):

      * ("W9ML", "strong") — explicit self-ID: "this is W9ML" / "de W9ML",
        or the FCC sign-off pattern (the over's only callsign spoken as its
        final words: "...I'll be in and out. N1LST"). May drive an
        assignment, subject to the voice veto.
      * ("W9ML", "weak")   — probable but unproven: the lone callsign in a
        non-addressing over, or the LAST of several callsigns (the calling
        convention puts the called station first, the caller last). Weak
        evidence must never assign or enroll on its own — it only names
        suggestions and corroborates a voice match.
      * (None, None)       — addressing phrases present, a roll call
        (many callsigns), or nothing heard.
    """
    calls = extract_callsigns(text)
    if not calls:
        return None, None
    up = " ".join(_candidates(text))
    for marker in ("THIS IS", " DE "):
        idx = up.rfind(marker)
        if idx >= 0:
            after = up[idx + len(marker):]
            m = CALL_RE.search(after)
            if m and m.group() in calls:
                return m.group(), "strong"
    if _ADDRESS_RE.search(text):
        return None, None
    if len(calls) == 1:
        # sign-off: a lone callsign spoken as the FINAL words of a
        # substantive over is the FCC-mandated self-ID ("...before it's
        # dark. KQ4ZMX"). Spelling the call PHONETICALLY is deliberate
        # self-ID even when short ("Kilo Charlie 8 Lima Uniform Bravo
        # Mobile" is only 7 words). Short plain-text or question-ending
        # overs stay weak — a bare "W9PRK?" is someone CALLING W9PRK.
        words = text.split()
        phonetic = sum(
            1 for w in words
            if re.sub(r"\W+", "", w).lower() in _PHONETIC) >= 3
        # 12-word tail: phonetic callsigns alone span 6+ words, plus
        # sign-off trailers like "in and out" / "clear and monitoring"
        if ((len(words) >= 8 or phonetic)
                and not text.rstrip().endswith("?")
                and calls[0] in extract_callsigns(" ".join(words[-12:]))):
            return calls[0], "strong"
        return calls[0], "weak"
    if len(calls) <= 3:
        # calling convention: "CALLED, CALLER ..." -> last call is probably
        # the person talking
        return calls[-1], "weak"
    return None, None                      # roll call / net check-in list


def _lev(a: str, b: str) -> int:
    """Levenshtein edit distance (small strings only)."""
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1,
                           prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _same_digit(a: str, b: str) -> bool:
    da = next((c for c in a if c.isdigit()), None)
    db = next((c for c in b if c.isdigit()), None)
    return da is not None and da == db


def reconcile_callsigns(calls, known) -> list[str]:
    """Snap mis-heard callsigns onto confirmed ones.

    Whisper mangles the phonetic prefix of a call ("Kilo Bravo" -> "T", so
    KB9ORJ comes out "T9ORJ"). When we already know who transmitted — a
    named, voice-matched speaker — any extracted call that's a close garble
    of their callsign (same digit, within two edits) is corrected to the
    real one. De-duplicated, order preserved. `known` may be a str or list.
    """
    if isinstance(known, str):
        known = [known]
    known = [k for k in known if k]
    if not known:
        return list(calls)
    out, seen = [], set()
    for c in calls:
        fixed = c
        for k in known:
            if c != k and _same_digit(c, k) and _lev(c, k) <= 2:
                fixed = k
                break
        if fixed not in seen:
            seen.add(fixed)
            out.append(fixed)
    return out
