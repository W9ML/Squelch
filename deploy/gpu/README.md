# GPU service drop-ins (NVIDIA / CUDA)

These make faster-whisper reliable on an NVIDIA GPU. `install.sh` installs them
automatically when `nvidia-smi` is present (and **regenerates `gpu.conf`** from
the actual venv). They're vendored here as the source of truth and for manual or
reference use. A CPU-only box needs none of this — set `device = "cpu"` under
`[transcribe]`.

The systemd drop-ins belong in `/etc/systemd/system/squelch.service.d/` (they
layer onto `deploy/squelch.service`).

- **`nvidia-uvm-ensure`** → `/usr/local/bin/` (mode 0755). Creates and verifies
  `/dev/nvidia-uvm` before start. Some boots load the NVIDIA driver but never
  create the UVM node, so CUDA dies with *"CUDA failed with error unknown error"*
  and nothing transcribes until someone runs `nvidia-smi` by hand. Retries while
  the driver settles; non-fatal so the web UI still comes up on a truly dead GPU.
- **`uvm-init.conf`** — runs the helper as `ExecStartPre` after the driver +
  `nvidia-persistenced` are up.
- **`gpu.conf`** — puts the venv's bundled `nvidia/*/lib` dirs on
  `LD_LIBRARY_PATH`. The copy here is for a Debian-13 (python3.13) `/opt/squelch`
  install; **install.sh regenerates it** for the real venv (python version + the
  exact nvidia wheels torch pulled), so treat this file as a reference.
- **`watchdog.conf`** — systemd `WatchdogSec=180`; the app pings while the
  pipeline is healthy, so a wedged event loop or a sustained CUDA fault gets
  killed and (`Restart=always`) restarted.

## Manual install (if not using install.sh)

```bash
install -m 0755 nvidia-uvm-ensure /usr/local/bin/nvidia-uvm-ensure
cp uvm-init.conf watchdog.conf gpu.conf /etc/systemd/system/squelch.service.d/
# regenerate gpu.conf for this box's venv (recommended):
SP=$(/opt/squelch/venv/bin/python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')
printf '[Service]\nEnvironment=LD_LIBRARY_PATH=%s\n' \
  "$(ls -d "$SP"/nvidia/*/lib | paste -sd: -)" > /etc/systemd/system/squelch.service.d/gpu.conf
systemctl daemon-reload && systemctl restart squelch
```
