#!/usr/bin/env bash
# squelch installer for Debian 13 (trixie). Run as root from the repo root:
#   sudo ./install.sh
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "run as root: sudo ./install.sh" >&2
    exit 1
fi

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR=/opt/squelch/app
VENV_DIR=/opt/squelch/venv
DATA_DIR=/var/lib/squelch
CONF_DIR=/etc/squelch

echo "==> installing OS packages"
apt-get update
# ffmpeg powers the admin "download MP3" feature; optional but small
apt-get install -y python3 python3-venv python3-dev build-essential rsync ffmpeg

echo "==> creating squelch user"
id -u squelch &>/dev/null || useradd --system --home-dir /var/lib/squelch --shell /usr/sbin/nologin squelch

echo "==> copying application to $APP_DIR"
mkdir -p "$APP_DIR"
rsync -a --delete \
    --exclude '.git' --exclude '__pycache__' --exclude '.venv' \
    "$SRC_DIR/squelch" "$SRC_DIR/tools" "$SRC_DIR/requirements.txt" "$APP_DIR/"

# the built Next.js frontend (static export). A release tarball ships web/out
# prebuilt; a bare git checkout does NOT (web/out is .gitignored), so build it
# here — installing Node.js if needed — to keep `git clone && ./install.sh`
# one-shot. A release install skips the build (web/out already present).
if [[ ! -d "$SRC_DIR/web/out" && -f "$SRC_DIR/web/package.json" ]]; then
    echo "==> web/out not present — building the frontend from source"
    if ! command -v npm >/dev/null 2>&1; then
        echo "    installing Node.js + npm"
        apt-get install -y nodejs npm
    fi
    ( cd "$SRC_DIR/web" && npm ci && npm run build ) \
        || echo "    WARNING: frontend build failed — the web UI won't load until you build web/out"
fi
if [[ -d "$SRC_DIR/web/out" ]]; then
    mkdir -p "$APP_DIR/web"
    rsync -a --delete "$SRC_DIR/web/out" "$APP_DIR/web/"
else
    echo "    WARNING: web/out missing — the web UI will not load until you"
    echo "             build it (cd web && npm ci && npm run build) and re-run."
fi

echo "==> python virtualenv + dependencies (this downloads ~2 GB of ML"
echo "    packages the first time; be patient)"
if [[ ! -d "$VENV_DIR" ]]; then
    python3 -m venv "$VENV_DIR"
fi
"$VENV_DIR/bin/pip" install --upgrade pip wheel
"$VENV_DIR/bin/pip" install -r "$APP_DIR/requirements.txt"

echo "==> data + config directories"
mkdir -p "$DATA_DIR" "$CONF_DIR"
chown -R squelch:squelch "$DATA_DIR"
if [[ ! -f "$CONF_DIR/squelch.toml" ]]; then
    cp "$SRC_DIR/config.example.toml" "$CONF_DIR/squelch.toml"
    chown root:squelch "$CONF_DIR/squelch.toml"
    chmod 640 "$CONF_DIR/squelch.toml"
    echo "    -> created $CONF_DIR/squelch.toml  (EDIT THIS: node number,"
    echo "       callsign, admin password)"
fi

# GPU acceleration (optional): if an NVIDIA GPU is present, install the systemd
# drop-ins that make CUDA reliable — the venv's bundled nvidia libs on
# LD_LIBRARY_PATH, the boot-time UVM-node fix (faster-whisper dies if
# /dev/nvidia-uvm is missing), and the watchdog. Skipped on a CPU-only box
# (set device = "cpu" under [transcribe]). See deploy/gpu/README.md.
if command -v nvidia-smi >/dev/null 2>&1 && [[ -d "$SRC_DIR/deploy/gpu" ]]; then
    echo "==> NVIDIA GPU detected — installing CUDA service drop-ins"
    DROPIN=/etc/systemd/system/squelch.service.d
    mkdir -p "$DROPIN"
    install -m 0755 "$SRC_DIR/deploy/gpu/nvidia-uvm-ensure" /usr/local/bin/nvidia-uvm-ensure
    cp "$SRC_DIR/deploy/gpu/uvm-init.conf" "$DROPIN/uvm-init.conf"
    cp "$SRC_DIR/deploy/gpu/watchdog.conf" "$DROPIN/watchdog.conf"
    # gpu.conf's LD_LIBRARY_PATH depends on the venv python version and which
    # nvidia wheels torch pulled, so generate it from THIS venv.
    SP="$("$VENV_DIR/bin/python" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
    NVLIBS="$(ls -d "$SP"/nvidia/*/lib 2>/dev/null | paste -sd: - || true)"
    if [[ -n "$NVLIBS" ]]; then
        printf '[Service]\nEnvironment=LD_LIBRARY_PATH=%s\n' "$NVLIBS" > "$DROPIN/gpu.conf"
        echo "    gpu.conf LD_LIBRARY_PATH -> $NVLIBS"
    else
        echo "    NOTE: no nvidia/*/lib in the venv (CPU-only torch?) — skipping gpu.conf"
    fi
fi

echo "==> systemd service"
cp "$SRC_DIR/deploy/squelch.service" /etc/systemd/system/squelch.service
systemctl daemon-reload
systemctl enable squelch

# Firewall: a default-deny firewall will silently drop the node's audio
# (UDP 32001) while the web port still works, which is baffling to debug.
# Detect ufw and open the audio port from the local subnet.
FW_NOTE=""
if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then
    SUBNET="$(ip -o -4 route show scope link 2>/dev/null | awk '/src/{print $1; exit}')"
    if [[ -n "$SUBNET" ]]; then
        echo "==> ufw is active — opening UDP 32001 (audio) from $SUBNET"
        ufw allow from "$SUBNET" to any port 32001 proto udp >/dev/null || true
    else
        FW_NOTE="ufw is active but the subnet could not be detected — run:
       sudo ufw allow to any port 32001 proto udp"
    fi
fi

cat <<EOF

Done. Next steps:
  1. Edit /etc/squelch/squelch.toml (node number, callsign, admin_password)
  2. systemctl start squelch
  3. Browse to http://<this-vm>:8080
  4. Configure the Pi to stream audio here (see README, "AllStar node setup")

  NOTE: the node's audio arrives on UDP 32001. If a firewall is enabled
  here, it MUST allow that port from the node, or audio silently never
  arrives (the web page still works, which makes this confusing).
  ${FW_NOTE}

Test without the radio:
  /opt/squelch/venv/bin/python /opt/squelch/app/tools/gen_mdc_wav.py /tmp/test.wav
  /opt/squelch/venv/bin/python /opt/squelch/app/tools/send_wav.py /tmp/test.wav
EOF
