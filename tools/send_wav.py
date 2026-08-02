#!/usr/bin/env python3
"""Stream a WAV file to squelch as chan_usrp-style UDP frames.

Simulates an AllStar node keying up, playing the file, and unkeying —
lets you test the whole pipeline without a radio:

    python tools/send_wav.py recording.wav --host 127.0.0.1 --port 32001

Accepts any mono/stereo 16-bit WAV; audio is mixed down and resampled
to 8 kHz as needed.
"""

import argparse
import socket
import struct
import time
import wave

import numpy as np

HEADER = struct.Struct(">4sIIIIIII")
FRAME = 160  # samples per 20 ms frame at 8 kHz


def load_wav_8k(path: str) -> np.ndarray:
    with wave.open(path, "rb") as w:
        nch, sw, rate, nframes = (w.getnchannels(), w.getsampwidth(),
                                  w.getframerate(), w.getnframes())
        if sw != 2:
            raise SystemExit(f"{path}: need 16-bit PCM (got {sw*8}-bit)")
        pcm = np.frombuffer(w.readframes(nframes), dtype="<i2")
    if nch > 1:
        pcm = pcm.reshape(-1, nch).mean(axis=1).astype(np.int16)
    if rate != 8000:
        try:
            from scipy.signal import resample_poly
        except ImportError:
            raise SystemExit(f"{path} is {rate} Hz; install scipy to resample")
        from math import gcd
        g = gcd(8000, rate)
        pcm = resample_poly(pcm.astype(np.float64), 8000 // g, rate // g)
        pcm = np.clip(pcm, -32768, 32767).astype(np.int16)
    return pcm


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("wav", help="WAV file to send")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=32001)
    ap.add_argument("--gap", type=float, default=0.0,
                    help="seconds of silence gap to insert mid-file (test "
                         "squelch-tail segmentation)")
    args = ap.parse_args()

    pcm = load_wav_8k(args.wav)
    pad = (-len(pcm)) % FRAME
    if pad:
        pcm = np.concatenate([pcm, np.zeros(pad, dtype=np.int16)])

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    dest = (args.host, args.port)
    seq = 0
    nframes = len(pcm) // FRAME
    print(f"sending {len(pcm)/8000:.1f}s ({nframes} frames) to {dest[0]}:{dest[1]}")

    t0 = time.monotonic()
    for i in range(nframes):
        chunk = pcm[i * FRAME:(i + 1) * FRAME]
        head = HEADER.pack(b"USRP", seq, 0, 1, 0, 0, 0, 0)
        sock.sendto(head + chunk.astype("<i2").tobytes(), dest)
        seq += 1
        if args.gap and i == nframes // 2:
            print(f"...{args.gap}s gap...")
            time.sleep(args.gap)
            t0 = time.monotonic() - (i + 1) * 0.02
        # pace to real time
        target = t0 + (i + 1) * 0.02
        delay = target - time.monotonic()
        if delay > 0:
            time.sleep(delay)

    sock.sendto(HEADER.pack(b"USRP", seq, 0, 0, 0, 0, 0, 0), dest)  # unkey
    print("done (sent unkey)")


if __name__ == "__main__":
    main()
