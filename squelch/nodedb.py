"""AllStarLink node database: node number -> callsign/description.

Fetches the same public dump Allmon uses (node|callsign|description|
location) once a day, cached on disk so lookups survive restarts and
network trouble. Private nodes (not registered with AllStarLink) can be
named via [node.aliases] in the config.
"""

from __future__ import annotations

import asyncio
import logging
import time
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)

ASTDB_URL = "http://allmondb.allstarlink.org/"
REFRESH_SECS = 24 * 3600
FETCH_TIMEOUT = 30


def parse_astdb(text: str) -> dict[str, dict]:
    out = {}
    for line in text.splitlines():
        parts = line.split("|")
        if len(parts) >= 2 and parts[0].strip().isdigit():
            out[parts[0].strip()] = {
                "callsign": parts[1].strip(),
                "description": parts[2].strip() if len(parts) > 2 else "",
                "location": parts[3].strip() if len(parts) > 3 else "",
            }
    return out


class NodeDB:
    def __init__(self, data_dir: Path, aliases: dict[str, str] | None = None):
        self.cache_file = Path(data_dir) / "astdb.txt"
        self.aliases = aliases or {}
        self._db: dict[str, dict] = {}
        if self.cache_file.exists():
            try:
                self._db = parse_astdb(
                    self.cache_file.read_text(errors="replace"))
                log.info("node db: %d nodes from cache", len(self._db))
            except OSError as e:
                log.warning("node db cache unreadable: %s", e)

    def lookup(self, node: str) -> dict | None:
        node = str(node).strip()
        if node in self.aliases:
            return {"callsign": self.aliases[node], "description": "",
                    "location": "", "alias": True}
        return self._db.get(node)

    def _fetch(self) -> None:
        req = urllib.request.Request(ASTDB_URL,
                                     headers={"User-Agent": "squelch"})
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as r:
            text = r.read().decode(errors="replace")
        db = parse_astdb(text)
        if len(db) < 1000:      # sanity: don't clobber cache with junk
            raise ValueError(f"astdb looks wrong ({len(db)} entries)")
        tmp = self.cache_file.with_suffix(".tmp")
        tmp.write_text(text)
        tmp.replace(self.cache_file)
        self._db = db
        log.info("node db: refreshed, %d nodes", len(db))

    async def run_refresher(self) -> None:
        while True:
            try:
                age = time.time() - self.cache_file.stat().st_mtime \
                    if self.cache_file.exists() else 1e12
                if age > REFRESH_SECS:
                    await asyncio.to_thread(self._fetch)
            except Exception as e:
                log.warning("node db refresh failed (keeping cache): %s", e)
            await asyncio.sleep(3600)
