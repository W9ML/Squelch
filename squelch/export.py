"""Time Machine export — stitch a time range of per-over WAVs into one MP4
(a waveform video with burned-in captions), rendered by a background worker
that yields to live whisper transcription so it never starves the pipeline.

Design notes:
- Audio is stitched in Python (numpy): each over is placed on one continuous
  timeline at ``started_at - range_start`` seconds, so the gaps the recorder
  never captured become real silence and the clip lines up with the DVR clock.
- Captions come from each over's word timestamps (0-based within the over),
  re-based onto the stitched timeline and burned via ffmpeg's ``subtitles``
  filter; overs with no word timings fall back to their whole transcript.
- The heavy lift is a single niced ffmpeg subprocess. The worker waits for the
  whisper queue to drain (and no over in progress) before it starts, and the
  ``nice -n 19`` child yields the core to live transcription if timing overlaps.
- Jobs are ephemeral: status lives in an in-memory dict, finished files under
  ``data_dir/exports`` (the only writable tree under the systemd sandbox).
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import shutil
import wave
from pathlib import Path

import numpy as np

from .usrp import SAMPLE_RATE

log = logging.getLogger(__name__)


class Exporter:
    """Owns the export queue, the in-memory job registry, and the render
    worker. One instance is created in ``web.py`` and its ``run_worker`` is
    registered alongside the pipeline's other lifespan tasks."""

    def __init__(self, cfg, db, pipeline):
        self.cfg = cfg
        self.db = db
        self.pipeline = pipeline
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self.jobs: dict[str, dict] = {}
        self._dir = cfg.data_dir / "exports"

    # ---- public API (called from the web layer) ----
    def submit(self, start: float, end: float, captions: bool) -> str:
        job_id = secrets.token_hex(6)
        self.jobs[job_id] = {
            "id": job_id, "status": "queued", "progress": 0.0,
            "start": float(start), "end": float(end),
            "captions": bool(captions), "dur": max(0.0, float(end) - float(start)),
            "error": None, "path": None,
        }
        self._queue.put_nowait(job_id)
        # bound the in-memory registry: evict the oldest terminal jobs (dict
        # keeps insertion order) so a long-lived process doesn't grow forever
        if len(self.jobs) > 200:
            for jid in list(self.jobs):
                if len(self.jobs) <= 200:
                    break
                if jid != job_id and self.jobs[jid]["status"] in ("done", "error"):
                    self.jobs.pop(jid, None)
        return job_id

    def status(self, job_id: str) -> dict | None:
        j = self.jobs.get(job_id)
        if not j:
            return None
        return {k: j[k] for k in ("id", "status", "progress", "dur", "error")}

    def file_path(self, job_id: str) -> str | None:
        j = self.jobs.get(job_id)
        if not j or j["status"] != "done" or not j["path"]:
            return None
        return j["path"] if Path(j["path"]).exists() else None

    # ---- worker ----
    async def run_worker(self) -> None:
        while True:
            job_id = await self._queue.get()
            try:
                await self._render(job_id)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — never let one job wedge the worker
                log.exception("export %s failed", job_id)
                j = self.jobs.get(job_id)
                if j:
                    j["status"], j["error"] = "error", str(e)
            finally:
                self._queue.task_done()

    async def _render(self, job_id: str) -> None:
        j = self.jobs[job_id]
        if not shutil.which("ffmpeg"):
            j["status"], j["error"] = "error", "ffmpeg is not installed on the server"
            return
        # yield to live transcription: never render while overs are queued or a
        # transmission is in progress, so the whisper worker always wins the CPU
        while self.pipeline.queue_depth > 0 or self.pipeline.rx_active:
            await asyncio.sleep(0.5)
        j["status"] = "rendering"
        overs = await asyncio.to_thread(
            self.db.overs_for_export, j["start"], j["end"])
        overs = [o for o in overs if o.get("audio_path")]
        if not overs:
            j["status"], j["error"] = "error", "no audio in that time range"
            return
        self._dir.mkdir(parents=True, exist_ok=True)
        scratch = self._dir / f"job_{job_id}"
        scratch.mkdir(parents=True, exist_ok=True)
        try:
            wav = scratch / "stitch.wav"
            total_s = await asyncio.to_thread(
                self._stitch, overs, j["start"], j["end"], wav)
            srt = scratch / "captions.srt"
            has_caps = bool(j["captions"]) and await asyncio.to_thread(
                self._write_srt, overs, j["start"], srt)
            out = self._dir / f"export_{job_id}.mp4"
            await self._ffmpeg(scratch, wav, srt if has_caps else None,
                               out, total_s, j)
            j["path"], j["status"], j["progress"] = str(out), "done", 1.0
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
            # a failed render leaves a partial/zero-byte mp4 that no one can
            # download (file_path() only serves 'done' jobs) — remove it, and
            # always prune so partials can't accumulate across failures
            if self.jobs.get(job_id, {}).get("status") != "done":
                (self._dir / f"export_{job_id}.mp4").unlink(missing_ok=True)
            self._prune()

    # ---- stitch (blocking, run via to_thread) ----
    def _stitch(self, overs: list[dict], start: float, end: float,
                out_path: Path) -> float:
        span = max(0.5, end - start)
        n = int(span * SAMPLE_RATE)
        buf = np.zeros(n, dtype=np.int16)
        for o in overs:
            p = o.get("audio_path")
            if not p or not Path(p).exists():
                continue
            try:
                with wave.open(str(p), "rb") as w:
                    fr = w.getframerate()
                    frames = w.readframes(w.getnframes())
                data = np.frombuffer(frames, dtype="<i2")
                if not len(data):
                    continue
                if fr != SAMPLE_RATE:
                    # cheap linear resample to the timeline rate
                    m = max(1, int(round(len(data) * SAMPLE_RATE / fr)))
                    data = np.interp(
                        np.linspace(0, len(data) - 1, m),
                        np.arange(len(data)), data).astype(np.int16)
                off = int(max(0.0, o["started_at"] - start) * SAMPLE_RATE)
                if off >= n:
                    continue
                seg = data[: n - off]
                buf[off: off + len(seg)] = seg
            except Exception:  # noqa: BLE001 — skip a single unreadable clip
                log.warning("export: skipping unreadable wav %s", p)
        with wave.open(str(out_path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SAMPLE_RATE)
            w.writeframes(buf.tobytes())
        return span

    # ---- captions (blocking) ----
    @staticmethod
    def _fmt_ts(t: float) -> str:
        # decompose from an integer millisecond count so rounding can never
        # carry into an invalid field (e.g. 00:01:60,000)
        total = max(0, int(round(t * 1000)))
        h, total = divmod(total, 3600000)
        m, total = divmod(total, 60000)
        s, ms = divmod(total, 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    def _write_srt(self, overs: list[dict], start: float, out_path: Path) -> bool:
        cues: list[tuple[float, float, str, str]] = []
        for o in overs:
            off = max(0.0, o["started_at"] - start)
            label = (o.get("speaker_label") or "").strip()
            words = o.get("words")
            if words:
                chunk: list = []
                for w in words:
                    chunk.append(w)
                    if len(chunk) >= 7:
                        cues.append((off + chunk[0][1], off + chunk[-1][2],
                                     label, " ".join(str(c[0]) for c in chunk)))
                        chunk = []
                if chunk:
                    cues.append((off + chunk[0][1], off + chunk[-1][2],
                                 label, " ".join(str(c[0]) for c in chunk)))
            elif (o.get("transcript") or "").strip():
                end_at = o.get("ended_at") or o["started_at"]
                dur = max(0.6, end_at - o["started_at"])
                cues.append((off, off + dur, label, o["transcript"].strip()))
        if not cues:
            return False
        blocks = []
        for i, (a, b, label, text) in enumerate(cues, 1):
            if b <= a:
                b = a + 0.6
            head = f"[{label}] " if label else ""
            blocks.append(f"{i}\n{self._fmt_ts(a)} --> {self._fmt_ts(b)}\n"
                          f"{head}{text}")
        out_path.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")
        return True

    # ---- render ----
    async def _ffmpeg(self, scratch: Path, wav: Path, srt: Path | None,
                      out: Path, total_s: float, job: dict) -> None:
        h = max(180, min(1080, int(self.cfg.export_video_height)))
        w = (int(h * 16 / 9)) // 2 * 2          # even width, 16:9
        wave_h = (h // 2) // 2 * 2
        # the color source is INFINITE; showwaves ends at audio EOF, so bound
        # the overlay (and the muxer, via -shortest below) to the audio or the
        # render never terminates.
        fc = (f"color=c=0x0d1117:s={w}x{h}:r=25[bg];"
              f"[0:a]showwaves=s={w}x{wave_h}:mode=cline:rate=25:"
              f"colors=0x6ea8fe[wv];"
              f"[bg][wv]overlay=(W-w)/2:(H-h)/2:shortest=1[base]")
        if srt is not None:
            fs = (f"FontName=DejaVu Sans Mono,Fontsize={max(14, h // 26)},"
                  f"PrimaryColour=&H00FFFFFF&,OutlineColour=&H90000000&,"
                  f"BorderStyle=1,Outline=2,Shadow=0,MarginV={h // 18}")
            fc += f";[base]subtitles={srt.name}:force_style='{fs}'[v]"
            vmap = "[v]"
        else:
            vmap = "[base]"
        args = [
            "nice", "-n", "19", "ffmpeg", "-hide_banner", "-v", "error",
            "-progress", "pipe:1", "-y", "-i", wav.name,
            "-filter_complex", fc, "-map", vmap, "-map", "0:a",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "24",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k",
            "-shortest", "-movflags", "+faststart", str(out),
        ]
        proc = await asyncio.create_subprocess_exec(
            *args, cwd=str(scratch),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        assert proc.stdout is not None
        err_chunks: list[bytes] = []

        async def _pump() -> None:
            # drain stderr concurrently so ffmpeg can never block on a full
            # stderr pipe while we're only reading stdout (classic deadlock)
            async def _drain_err() -> None:
                if proc.stderr is None:
                    return
                async for eline in proc.stderr:
                    err_chunks.append(eline)
            et = asyncio.create_task(_drain_err())
            async for raw in proc.stdout:  # type: ignore[union-attr]
                line = raw.decode(errors="ignore").strip()
                if line.startswith("out_time_us="):
                    try:
                        us = int(line.split("=", 1)[1])
                        job["progress"] = min(0.99, (us / 1e6) / max(0.5, total_s))
                    except ValueError:
                        pass
            await et
            await proc.wait()

        try:
            # -shortest already bounds the output length; this wall-clock cap is
            # a backstop so a wedged ffmpeg can never permanently block the queue
            await asyncio.wait_for(_pump(), timeout=max(120.0, total_s * 4))
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError("render timed out")
        if proc.returncode != 0:
            err = b"".join(err_chunks).decode(errors="ignore")
            raise RuntimeError(err.strip()[-300:] or "ffmpeg render failed")

    def _prune(self, keep: int = 12) -> None:
        try:
            files = sorted(self._dir.glob("export_*.mp4"),
                           key=lambda p: p.stat().st_mtime, reverse=True)
            for p in files[keep:]:
                p.unlink(missing_ok=True)
        except OSError:
            pass
