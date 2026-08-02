"""MDC-1200 decoder and encoder.

Python port of Matthew Kaufman's MDC Encoder/Decoder Library
(https://github.com/atmatthewat/mdc-encode-decode), using the
"fourpoint" differential detection strategy with 5 phase-offset
decode units, convolutional error correction, and the flipped,
inverted CCITT-16 CRC.

Because this module is derived from that GPLv2 library, squelch as a
whole is distributed under the GNU General Public License v2.

The decoder wants >= 16 kHz sample rates (same recommendation as the
reference fourpoint implementation); callers with 8 kHz audio should
upsample first (see decode_transmission()).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

_U32 = 0xFFFFFFFF

# Exact phase-increment constants from the reference implementation so
# bit timing matches it precisely at the common rates.
_INCRU_BY_RATE = {
    8000: 644245094,
    16000: 322122547,
    22050: 233739716,
    32000: 161061274,
    44100: 116869858,
    48000: 107374182,
}

_SYNC_HIGH = 0x07        # top 8 bits of the 40-bit frame sync
_SYNC_LOW = 0x092A446F   # bottom 32 bits
_GDTHRESH = 5            # tolerated bad bits in the 40-bit sync window
_ND = 5                  # number of phase-offset decode units

# Opcodes whose packets are doubles (a second 14-byte block follows
# carrying four extra bytes).
_DOUBLE_OPS = frozenset((0x35, 0x55))

# Human labels for the well-attested opcodes seen in amateur/commercial
# use. Anything else is shown as raw hex by MDCPacket.describe().
_OP_NAMES = {
    0x01: "PTT ID",
    0x03: "Remote Monitor",
    0x35: "Call Alert",
    0x55: "Status/Message",
}

# op code -> the app_rpt-style type letter the forwarder path uses
# (I=PTT ID, E=Emergency, S=Status, C=Call). Speaker attribution and the
# unit->operator UI key on this, so audio-decoded packets must carry the
# same vocabulary as forwarded mdclog events.
OP_TYPES = {
    0x00: "E",
    0x01: "I",
    0x35: "C",
    0x55: "S",
}


def _flip(value: int, bitnum: int) -> int:
    """Reverse the low `bitnum` bits of value."""
    out = 0
    for i in range(bitnum):
        if value & (1 << (bitnum - 1 - i)):
            out |= 1 << i
    return out


def _docrc(data: bytes) -> int:
    """MDC-1200 CRC: bit-flipped CCITT-16 over the payload, result
    bit-flipped and inverted."""
    crc = 0x0000
    for byte in data:
        c = _flip(byte, 8)
        for j in (0x80, 0x40, 0x20, 0x10, 0x08, 0x04, 0x02, 0x01):
            bit = crc & 0x8000
            crc = (crc << 1) & 0xFFFF
            if c & j:
                bit ^= 0x8000
            if bit:
                crc ^= 0x1021
    return (_flip(crc, 16) ^ 0xFFFF) & 0xFFFF


@dataclass
class MDCPacket:
    op: int
    arg: int
    unit_id: int
    extras: tuple[int, int, int, int] | None = None  # set for double packets

    def describe(self) -> str:
        name = _OP_NAMES.get(self.op, f"Op 0x{self.op:02X}")
        if self.op == 0x01:
            when = {0x80: " (pre)", 0x00: " (post)"}.get(self.arg, f" arg 0x{self.arg:02X}")
            return f"{name}{when}"
        return f"{name} arg 0x{self.arg:02X}"

    def to_dict(self) -> dict:
        d = {
            "source": "audio",
            "op": self.op,
            "arg": self.arg,
            "unit_id": self.unit_id,
            "unit_id_hex": f"{self.unit_id:04X}",
            # unit_raw/type mirror the forwarder entries (mdc_ingest.make_entry):
            # everything downstream — operator mapping, badge linking, unit
            # filtering — keys on these, regardless of which path decoded it
            "unit_raw": f"{self.unit_id:04X}",
            "label": self.describe(),
        }
        t = OP_TYPES.get(self.op)
        if t:
            d["type"] = t
        if self.extras is not None:
            d["extras"] = list(self.extras)
        return d


class _DecodeUnit:
    __slots__ = ("thu", "xorb", "invert", "nlstep", "nlevel",
                 "synclow", "synchigh", "shstate", "shcount", "bits")

    def __init__(self, index: int):
        self.thu = (index * 2 * (0x80000000 // _ND)) & _U32
        self.xorb = 0
        self.invert = 0
        self.nlstep = index
        self.nlevel = [0.0] * 10
        self.synclow = 0
        self.synchigh = 0
        self.shstate = -1
        self.shcount = 0
        self.bits = [0] * 112


class MDCDecoder:
    """Streaming MDC-1200 decoder. Feed 16-bit or float samples to
    process(); decoded packets accumulate in .packets."""

    def __init__(self, sample_rate: int):
        if sample_rate < 16000:
            raise ValueError("MDCDecoder needs >= 16 kHz; upsample first")
        self.sample_rate = sample_rate
        self.incru = _INCRU_BY_RATE.get(
            sample_rate, 1200 * 2 * (0x80000000 // sample_rate))
        self.units = [_DecodeUnit(i) for i in range(_ND)]
        self.indouble = False
        self._pending: MDCPacket | None = None
        self.packets: list[MDCPacket] = []

    # --- bit-level machinery (faithful to the reference) ---

    def _procbits(self, u: _DecodeUnit) -> None:
        # de-interleave the 112 received bits (16 columns x 7 rows)
        lbits = [0] * 112
        lbc = 0
        for i in range(16):
            for j in range(7):
                lbits[lbc] = u.bits[j * 16 + i]
                lbc += 1
        # pack LSB-first into 14 bytes
        data = bytearray(14)
        for i in range(14):
            b = 0
            for j in range(8):
                if lbits[i * 8 + j]:
                    b |= 1 << j
            data[i] = b

        _ecc_fix(data)

        ccrc = _docrc(bytes(data[:4]))
        rcrc = (data[5] << 8) | data[4]
        if ccrc != rcrc:
            u.shstate = -1
            return

        if u.shstate == 2:
            # second half of a double packet
            if self._pending is not None:
                pkt = self._pending
                pkt.extras = (data[0], data[1], data[2], data[3])
                self.packets.append(pkt)
            self._pending = None
            self.indouble = False
            for k in self.units:
                k.shstate = -1
            return

        if not self.indouble:
            pkt = MDCPacket(op=data[0], arg=data[1],
                            unit_id=(data[2] << 8) | data[3])
            if pkt.op in _DOUBLE_OPS:
                self.indouble = True
                self._pending = pkt
                u.shstate = 2
                u.shcount = 0
                u.bits = [0] * 112
            else:
                self.packets.append(pkt)
                for k in self.units:
                    k.shstate = -1
        else:
            # already saw the first half on another unit; let this unit
            # also try for the second half
            u.shstate = 2
            u.shcount = 0
            u.bits = [0] * 112

    def _shiftin(self, u: _DecodeUnit) -> None:
        bit = u.xorb
        if u.shstate == -1:
            u.synchigh = 0
            u.synclow = 0
            u.shstate = 0
        if u.shstate == 0:
            u.synchigh = ((u.synchigh << 1) & _U32)
            if u.synclow & 0x80000000:
                u.synchigh |= 1
            u.synclow = (u.synclow << 1) & _U32
            if bit:
                u.synclow |= 1
            gcount = bin(0xFF & (_SYNC_HIGH ^ u.synchigh)).count("1")
            gcount += bin(_SYNC_LOW ^ u.synclow).count("1")
            if gcount <= _GDTHRESH:
                u.shstate = 1
                u.shcount = 0
                u.bits = [0] * 112
            elif gcount >= (40 - _GDTHRESH):
                u.shstate = 1
                u.shcount = 0
                u.xorb = 0 if u.xorb else 1
                u.invert = 0 if u.invert else 1
                u.bits = [0] * 112
            return
        if u.shstate in (1, 2):
            u.bits[u.shcount] = bit
            u.shcount += 1
            if u.shcount > 111:
                self._procbits(u)

    def _nlproc(self, u: _DecodeUnit) -> None:
        nl = u.nlevel
        if u.nlstep == 3:
            vnow = (-0.60 * nl[3]) + (0.97 * nl[1])
            vpast = (-0.60 * nl[7]) + (0.97 * nl[9])
        elif u.nlstep == 8:
            vnow = (-0.60 * nl[8]) + (0.97 * nl[6])
            vpast = (-0.60 * nl[2]) + (0.97 * nl[4])
        else:
            return
        u.xorb = 1 if vnow > vpast else 0
        if u.invert:
            u.xorb = 0 if u.xorb else 1
        self._shiftin(u)

    # --- sample-level entry point ---

    def process(self, samples: np.ndarray) -> list[MDCPacket]:
        """Process a chunk of samples. int16 input is scaled the same
        way as the reference (x/65536); float input is used as-is.
        Returns packets decoded so far (also kept in .packets)."""
        if samples.dtype == np.int16:
            values = samples.astype(np.float64) / 65536.0
        else:
            values = np.asarray(samples, dtype=np.float64)
        n = len(values)
        if n == 0:
            return self.packets

        # The reference advances, per unit, a 32-bit phase accumulator
        # by `step` per sample and takes a level reading on each
        # wraparound. Compute those wrap positions directly, then merge
        # all units' readings into one chronological stream so that
        # cross-unit state (resets after a good decode, double-packet
        # handling) behaves exactly like the per-sample C loop.
        step = (5 * self.incru) & _U32
        two32 = 1 << 32
        ev_idx, ev_unit = [], []
        for ui, u in enumerate(self.units):
            thu0 = u.thu
            total = thu0 + n * step
            nwraps = total // two32
            if nwraps:
                ks = np.arange(1, nwraps + 1, dtype=np.int64)
                idx = -((thu0 - ks * two32) // step) - 1  # ceil div, then -1
                ev_idx.append(idx)
                ev_unit.append(np.full(len(idx), ui, dtype=np.int64))
            u.thu = total & _U32
        if not ev_idx:
            return self.packets

        idxs = np.concatenate(ev_idx)
        uids = np.concatenate(ev_unit)
        order = np.lexsort((uids, idxs))
        vals = values[idxs[order]]
        uids = uids[order]

        units = self.units
        nlproc = self._nlproc
        for i in range(len(vals)):
            u = units[uids[i]]
            u.nlstep += 1
            if u.nlstep > 9:
                u.nlstep = 0
            u.nlevel[u.nlstep] = vals[i]
            if u.nlstep == 3 or u.nlstep == 8:
                nlproc(u)

        return self.packets

    def flush(self) -> list[MDCPacket]:
        """Feed a short zero tail so decisions pending at the very end
        of a signal (the detector lags the waveform by up to a bit
        period) are pushed through. Call after the last real samples of
        a recording; not needed mid-stream."""
        self.process(np.zeros(int(0.05 * self.sample_rate)))
        return self.packets


def _ecc_fix(data: bytearray) -> None:
    """Syndrome-based single-error correction for the (16,8)
    convolutional code protecting the first 7 bytes."""
    syn = 0
    csr = [0] * 7
    for i in range(7):
        for j in range(8):
            for k in range(6, 0, -1):
                csr[k] = csr[k - 1]
            csr[0] = (data[i] >> j) & 0x01
            b = csr[0] + csr[2] + csr[5] + csr[6]
            syn = (syn << 1) & 0xFF
            if (b & 0x01) ^ ((data[i + 7] >> j) & 0x01):
                syn |= 1
            ec = 0
            for mask in (0x80, 0x20, 0x04, 0x02):
                if syn & mask:
                    ec += 1
            if ec >= 3:
                syn ^= 0xA6
                fixi, fixj = i, j - 7
                if fixj < 0:
                    fixi -= 1
                    fixj += 8
                if fixi >= 0:
                    data[fixi] ^= 1 << fixj


# --------------------------------------------------------------------
# Encoder (used by the test suite and tools/gen_mdc_wav.py)
# --------------------------------------------------------------------

def _enc_block(payload4: bytes) -> bytes:
    """CRC + convolutional-encode + interleave 4 payload bytes into the
    14 bytes that go over the air."""
    data = bytearray(14)
    data[0:4] = payload4
    crc = _docrc(payload4)
    data[4] = crc & 0xFF
    data[5] = (crc >> 8) & 0xFF
    data[6] = 0
    csr = [0] * 7
    for i in range(7):
        for j in range(8):
            for k in range(6, 0, -1):
                csr[k] = csr[k - 1]
            csr[0] = (data[i] >> j) & 0x01
            b = csr[0] + csr[2] + csr[5] + csr[6]
            data[i + 7] |= (b & 0x01) << j
    # interleave: scatter LSB-first bits with stride 16 ...
    lbits = [0] * 112
    k = m = 0
    for i in range(14):
        for j in range(8):
            lbits[k] = 0x01 & (data[i] >> j)
            k += 16
            if k > 111:
                m += 1
                k = m
    # ... then repack MSB-first for transmission
    out = bytearray(14)
    k = 0
    for i in range(14):
        for j in range(7, -1, -1):
            if lbits[k]:
                out[i] |= 1 << j
            k += 1
    return bytes(out)


def encode_packet(op: int, arg: int, unit_id: int,
                  sample_rate: int = 8000,
                  extras: tuple[int, int, int, int] | None = None,
                  preamble_bytes: int = 0,
                  amplitude: float = 0.65) -> np.ndarray:
    """Generate an MDC-1200 burst as int16 samples (XOR-precoded MSK,
    1200/1800 Hz). `extras` makes it a double packet."""
    leader = bytes([0x55] * 7) + bytes([0x07, 0x09, 0x2A, 0x44, 0x6F])
    payload = bytes([op & 0xFF, arg & 0xFF, (unit_id >> 8) & 0xFF, unit_id & 0xFF])
    data = leader + _enc_block(payload)
    if extras is not None:
        data += _enc_block(bytes(x & 0xFF for x in extras))

    incru = _INCRU_BY_RATE.get(sample_rate, 1200 * 2 * (0x80000000 // sample_rate))
    incru18 = int(incru * 1.5) & _U32
    if sample_rate in _INCRU_BY_RATE:
        incru18 = {8000: 966367642, 16000: 483183820, 22050: 350609575,
                   32000: 241591910, 44100: 175304788, 48000: 161061274}[sample_rate]

    out = []
    thu = tthu = 0
    bpos = ipos = 0
    xorb = 1
    lb = 0
    pre = preamble_bytes
    scale = amplitude * 32767.0
    two_pi_over = 2.0 * np.pi / 4294967296.0
    while True:
        lthu = thu
        thu = (thu + incru) & _U32
        if thu < lthu:  # bit clock wrapped: advance to next data bit
            ipos += 1
            if ipos > 7:
                ipos = 0
                if pre == 0:
                    bpos += 1
                else:
                    pre -= 1
                if bpos >= len(data):
                    break
            b = 0x01 & (data[bpos] >> (7 - ipos))
            if b != lb:
                xorb = 1
                lb = b
            else:
                xorb = 0
        tthu = (tthu + (incru18 if xorb else incru)) & _U32
        out.append(np.sin(tthu * two_pi_over))
    return (np.array(out) * scale).astype(np.int16)


def decode_transmission(samples_8k: np.ndarray, sample_rate: int = 8000) -> list[dict]:
    """Decode all MDC-1200 packets in a complete transmission.
    Upsamples to 16 kHz for the fourpoint detector when needed."""
    from scipy.signal import resample_poly

    if sample_rate < 16000:
        factor = 16000 // sample_rate
        audio = resample_poly(samples_8k.astype(np.float64) / 65536.0, factor, 1)
        rate = sample_rate * factor
    else:
        audio = samples_8k.astype(np.float64) / 65536.0
        rate = sample_rate
    dec = MDCDecoder(rate)
    dec.process(audio)
    dec.flush()
    # a burst cut off mid-double still reports its first half
    if dec._pending is not None:
        dec.packets.append(dec._pending)
    return [p.to_dict() for p in dec.packets]
