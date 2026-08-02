#!/usr/bin/env python3
"""Capture the raw chan_usrp stream to a WAV and decode MDC from it.

Use this to see exactly what audio the VM receives from the node,
independent of the squelch service. squelch binds the USRP port, so stop it
first:

    sudo systemctl stop squelch
    /opt/squelch/venv/bin/python /opt/squelch/app/tools/dump_usrp.py --port 32001

Then key up the radio (send your MDC ID) a few times and press Ctrl+C.
It writes capture.wav (8 kHz mono) and reports any MDC packets found,
with their timing, so we can tell whether the burst arrives at all and
whether it's clipped at the start.

    sudo systemctl start squelch      # when done
"""

import argparse
import signal
import socket
import struct
import sys
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from squelch.mdc1200 import MDCDecoder  # noqa: E402
from scipy.signal import resample_poly  # noqa: E402

HEADER = struct.Struct(">4sIIIIIII")
SAMPLE_RATE = 8000
USRP_TYPE_VOICE = 0

running = True


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=32001)
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--out", default="capture.wav")
    ap.add_argument("--seconds", type=float, default=0,
                    help="auto-stop after this many seconds instead of "
                         "waiting for Ctrl+C")
    args = ap.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((args.bind, args.port))
    sock.settimeout(0.5)

    def stop(*_):
        global running
        running = False
    signal.signal(signal.SIGINT, stop)

    import time
    deadline = time.monotonic() + args.seconds if args.seconds else None
    if deadline:
        print(f"listening on {args.bind}:{args.port} for {args.seconds:.0f}s "
              f"— key up the radio (with MDC) now")
    else:
        print(f"listening on {args.bind}:{args.port} — key up the radio "
              f"(with MDC), Ctrl+C when done")

    chunks = []
    voice_frames = 0
    keyups = 0
    prev_keyed = False
    while running:
        if deadline and time.monotonic() >= deadline:
            break
        try:
            data, _ = sock.recvfrom(2048)
        except socket.timeout:
            continue
        if len(data) < HEADER.size:
            continue
        eye, seq, _mem, keyup, _tg, ftype, _mpx, _rsv = HEADER.unpack_from(data)
        if eye != b"USRP":
            continue
        if keyup and not prev_keyed:
            keyups += 1
        prev_keyed = bool(keyup)
        if ftype == USRP_TYPE_VOICE and len(data) > HEADER.size:
            pcm = np.frombuffer(data[HEADER.size:HEADER.size + 320], dtype="<i2")
            if len(pcm):
                chunks.append(pcm)
                voice_frames += 1

    if not chunks:
        print("\nno voice audio captured — nothing was streaming")
        return

    audio = np.concatenate(chunks)
    secs = len(audio) / SAMPLE_RATE
    peak = int(np.max(np.abs(audio.astype(np.int32))))
    with wave.open(args.out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(audio.astype("<i2").tobytes())

    print(f"\ncaptured {secs:.1f}s ({voice_frames} frames, {keyups} keyups), "
          f"peak level {peak}/32767 -> {args.out}")

    # decode MDC over the whole capture (upsample to 16k like the pipeline)
    up = resample_poly(audio.astype(np.float64) / 65536.0, 2, 1)
    dec = MDCDecoder(16000)
    dec.process(up)
    dec.flush()
    if dec.packets:
        print(f"MDC: {len(dec.packets)} packet(s) decoded:")
        for p in dec.packets:
            print(f"   {p.describe()}  unit 0x{p.unit_id:04X}")
    else:
        print("MDC: none decoded in the captured audio")
        print("  -> if you clearly heard the blurp on a monitor radio but it's")
        print("     absent/faint here, the burst is being lost between the")
        print("     node and the link (leading-edge clip or filtering).")


if __name__ == "__main__":
    main()
