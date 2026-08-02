"""systemd watchdog integration (no external dependency).

`sd_notify` writes to the `$NOTIFY_SOCKET` unix datagram systemd provides; all
calls are no-ops when that isn't set (local runs, tests, non-systemd), so this
is safe to wire in unconditionally.

`run_pinger` sends `WATCHDOG=1` on the cadence systemd expects — but ONLY while
the supplied health callback reports healthy. That covers both failure modes
with one mechanism: a wedged event loop stops pinging on its own, and a
detected GPU/CUDA fault (Pipeline.healthy() -> False) deliberately withholds
pings — either way systemd's `WatchdogSec` trips and `Restart=always` brings
the process back with a fresh CUDA context. This is the "restart, don't rebuild
in-process" recovery: an in-process CUDA reinit typically faults again on
Pascal, a new process does not.

Requires `WatchdogSec=` in the unit (systemd then sets `WATCHDOG_USEC`). Works
with any service Type — it does not depend on Type=notify / READY=1.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from typing import Callable

log = logging.getLogger(__name__)


def sd_notify(state: str) -> None:
    """Send a state string (e.g. "WATCHDOG=1", "READY=1") to systemd. No-op
    when not running under systemd."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr[0] == "@":                       # abstract namespace socket
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(addr)
            sock.sendall(state.encode())
    except OSError as e:                      # never let telemetry break the app
        log.debug("sd_notify(%s) failed: %s", state, e)


async def run_pinger(healthy: Callable[[], bool]) -> None:
    """Ping the systemd watchdog every WATCHDOG_USEC/2 while healthy() is True.
    Returns immediately (does nothing) when the watchdog isn't configured."""
    usec = os.environ.get("WATCHDOG_USEC")
    if not os.environ.get("NOTIFY_SOCKET") or not usec:
        return
    try:
        interval = max(1.0, (int(usec) / 1_000_000) / 2.0)
    except ValueError:
        return
    sd_notify("WATCHDOG=1")                   # arm immediately on startup
    warned = False
    while True:
        await asyncio.sleep(interval)
        if healthy():
            sd_notify("WATCHDOG=1")
            warned = False
        elif not warned:
            log.critical("withholding systemd watchdog pings — expecting a "
                         "restart within ~%.0fs", interval * 2)
            warned = True
