#!/usr/bin/env python3
"""Generate a test WAV containing an MDC-1200 burst.

    python tools/gen_mdc_wav.py out.wav --unit 0x1234
    python tools/send_wav.py out.wav          # feed it to squelch

The output is 8 kHz mono: 0.3 s of silence, the MDC PTT-ID burst, then
two seconds of a soft 440 Hz tone standing in for speech.
"""

import argparse
import sys
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from squelch.mdc1200 import encode_packet  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("out", help="output WAV path")
    ap.add_argument("--op", type=lambda s: int(s, 0), default=0x01)
    ap.add_argument("--arg", type=lambda s: int(s, 0), default=0x00)
    ap.add_argument("--unit", type=lambda s: int(s, 0), default=0x1234)
    args = ap.parse_args()

    rate = 8000
    silence = np.zeros(int(0.3 * rate), dtype=np.int16)
    burst = encode_packet(args.op, args.arg, args.unit, sample_rate=rate)
    t = np.arange(2 * rate) / rate
    tone = (0.25 * 32767 * np.sin(2 * np.pi * 440 * t)).astype(np.int16)

    audio = np.concatenate([silence, burst, silence, tone])
    with wave.open(args.out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(audio.astype("<i2").tobytes())
    print(f"wrote {args.out}: MDC op=0x{args.op:02X} arg=0x{args.arg:02X} "
          f"unit=0x{args.unit:04X}, {len(audio)/rate:.1f}s")


if __name__ == "__main__":
    main()
