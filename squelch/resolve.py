"""Say Again — cross-source callsign resolution.

Whisper mangles callsigns (they aren't natural language, so its probabilities
are lowest exactly where hams care most), and a receive-only monitor can't key
up to ask "say again". This fuses the extracted call with INDEPENDENT evidence
— the attributed speaker's known call (weighted by HOW they were identified: an
MDC unit or a manual name is hard, a voiceprint is soft) and a strong spoken
self-ID — and asserts a correction only when the target carries enough
independent authority. QRZ/callook validity (from the local cache, no network)
is one more corroborating signal.

Everything is a soft annotation: an unverified call is shown AS HEARD with a
NATO spellback, never silently rewritten. A rewrite ("W9NL" -> "W9ML") happens
only when at least two independent sources back the corrected call.
"""

from __future__ import annotations

_PHON = {
    "A": "Alpha", "B": "Bravo", "C": "Charlie", "D": "Delta", "E": "Echo",
    "F": "Foxtrot", "G": "Golf", "H": "Hotel", "I": "India", "J": "Juliet",
    "K": "Kilo", "L": "Lima", "M": "Mike", "N": "November", "O": "Oscar",
    "P": "Papa", "Q": "Quebec", "R": "Romeo", "S": "Sierra", "T": "Tango",
    "U": "Uniform", "V": "Victor", "W": "Whiskey", "X": "X-ray", "Y": "Yankee",
    "Z": "Zulu", "0": "Zero", "1": "One", "2": "Two", "3": "Three", "4": "Four",
    "5": "Five", "6": "Six", "7": "Seven", "8": "Eight", "9": "Niner",
}


def nato_spell(call: str) -> str:
    """Phonetic spellback for a callsign: 'W9ML' -> 'Whiskey Niner Mike Lima'."""
    return " ".join(_PHON.get(ch, ch) for ch in call.upper())


def _lev(a: str, b: str) -> int:
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


def resolve(heard, id_calls, id_authority, self_call, is_valid) -> list[dict]:
    """Resolve extracted callsigns against independent evidence.

    heard         : extracted calls, transcript order (from extract_callsigns)
    id_calls      : set of calls from the attributed NAMED speaker's label
    id_authority  : 2 = hard identity (manual / MDC unit), 1 = soft (voiceprint
                    / spoken-callsign attribution), 0 = none/unnamed
    self_call     : a STRONG spoken self-ID call in this over, or None
    is_valid(call): True when `call` resolves in the QRZ/callook cache

    Returns a list of {heard, resolved, status, sources, spell} (+ alt/alt_spell
    on an 'uncertain'), de-duplicated by the resolved call, order preserved.

    status:
      corrected  — rewritten to a better-supported call (>=2 authority)
      confirmed  — heard call is backed by voice/self-ID (and maybe QRZ)
      valid      — heard call resolves in QRZ but isn't tied to this speaker
      uncertain  — a plausible correction exists but is too weakly supported
      unverified — heard as-is, nothing corroborates it
    """
    id_calls = set(id_calls or ())
    targets = set(id_calls)
    if self_call:
        targets.add(self_call)

    out: list[dict] = []
    seen: set[str] = set()
    for c in heard:
        # sources that assert c exactly as spoken
        srcs: list[str] = []
        if c in id_calls:
            srcs.append("voice")
        if self_call and c == self_call:
            srcs.append("self-ID")
        if is_valid(c):
            srcs.append("QRZ")

        # a close garble of one of the identity calls?
        target = None
        for t in targets:
            if t != c and _same_digit(c, t) and _lev(c, t) <= 2:
                target = t
                break

        status, resolved, sources = "unverified", c, srcs
        alt = None
        if target:
            auth, tsrc = 0, []
            if target in id_calls:
                auth += id_authority
                tsrc.append("voice")
            if self_call and target == self_call:
                auth += 1
                tsrc.append("self-ID")
            if is_valid(target):
                auth += 1
                tsrc.append("QRZ")
            if auth >= 2:
                status, resolved, sources = "corrected", target, tsrc
            else:
                status, alt = "uncertain", target
        elif srcs:
            if "QRZ" in srcs and ("voice" in srcs or "self-ID" in srcs):
                status = "confirmed"
            elif "QRZ" in srcs:
                status = "valid"
            else:
                status = "confirmed"   # voice/self-ID vouches for it

        if resolved in seen:
            continue
        seen.add(resolved)
        item = {"heard": c, "resolved": resolved, "status": status,
                "sources": sources, "spell": nato_spell(resolved)}
        if alt:
            item["alt"] = alt
            item["alt_spell"] = nato_spell(alt)
        out.append(item)
    return out
