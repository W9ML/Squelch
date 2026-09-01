# Squelch

**A self-hosted, receive-only web monitor for an AllStarLink (ham radio) node.**

Squelch listens to everything your repeater or node hears and turns it into a
live, searchable web feed — every transmission recorded, transcribed,
voice-identified, and badged — running entirely on **your own hardware**, with
nothing in the cloud. Viewing is public; naming speakers and changing settings
need an admin login.

By construction it is **receive-only**: it never sends audio back to the node, so
it can hear the repeater but can never key it up.

```
 Raspberry Pi (ASL3)                 Debian 13 VM
┌─────────────────────┐            ┌──────────────────────────────┐
│ node 546xx (radio)  │            │ squelch                        │
│        │ monitor    │  UDP/USRP  │  ┌─ segmenter ─ WAV store    │
│ node 1999 (USRP) ───┼───────────►│  ├─ MDC1200 decoder          │
│                     │ 8kHz PCM   │  ├─ Whisper transcription    │
└─────────────────────┘            │  ├─ voice embeddings/cluster │
                                   │  └─ FastAPI + WebSocket ──► browser
                                   └──────────────────────────────┘
```

## Features

- **Live feed** — a chat-style stream of transmissions with a clickable waveform
  player (peaks are precomputed, so waveforms survive audio auto-pruning), an
  ON&nbsp;AIR banner, and full-text transcript search.
- **Local transcription** — Whisper (faster-whisper) runs on your box; model is
  selectable in the UI (`tiny.en` → `large-v3`, CPU or NVIDIA GPU).
- **Streaming live captions** *(optional)* — provisional text appears while
  someone is still talking, replaced by the full transcript on unkey.
- **Voice identification** — recurring voices are clustered automatically; name
  one and every future match is labeled. Spoken **callsigns override voice** and
  auto-name new speakers (CPU Resemblyzer, or GPU TitaNet for higher accuracy).
- **Say Again** — cross-checks heard callsigns against known stations and repairs
  busted decodes (e.g. `W9DT` → `W9DTE`), with tap-to-loop playback.
- **MDC1200 PTT-ID** — decoded from the audio, *or* forwarded from app_rpt's own
  decoder to catch pre-burst ANI that many repeaters gate off-air.
- **DTMF decode** — a live keypad overlay of touch-tones.
- **Connection analytics** — an admin dashboard over the visitor log: top IPs, a
  when-active heatmap, session lengths, peak concurrent viewers, client breakdown.
- **Watchlist + push** — browser or webhook alerts when a callsign, unit ID,
  emergency, or speaker you care about hits the air.
- **Cases** *(admin)* — an investigative case file for documenting malicious
  interference: numbered cases, a suspected operator, evidence recordings pinned
  from any transmission's ⋯ menu (and **exempt from audio auto-pruning** while
  filed), an append-only activity log, and a printable or ZIP export (report +
  audio).
- **Interference triage** — a non-speech filter surfaces overs that carried
  audio but no recognized speech (carriers, tones, jamming) for quick review.
- **Search & stats** — faceted filtering, per-speaker pages, and an activity
  dashboard (busiest hours, top talkers, per-node breakdown).
- **Fuzzy TX geolocation** *(optional)* — on a voted/simulcast network, per-site
  RSSI drives an honest, uncertainty-aware location estimate (a cloud, not a pin).
- **Make it yours** — light/dark themes, uploadable logo, editable footer, and an
  installable PWA.

## Requirements

- **A Linux host** (Debian 13 is the reference; anything close works). 4+ cores
  and 6–8 GB RAM is comfortable for CPU Whisper (`base.en` / `small.en`);
  `tiny.en` runs on much less, and a Raspberry Pi 5 handles the light models
  fine. An **NVIDIA GPU is optional** and unlocks `large-v3` transcription plus
  TitaNet voice ID. Budget ~6 GB for the Python environment and Whisper models,
  plus audio storage (8 kHz WAV ≈ 1 MB per minute of talk time; auto-pruned after
  a configurable retention).
- **A node running AllStarLink 3** with UDP reachability to the host — Squelch
  and the node can even be the same machine.

## Install (on the VM)

```sh
git clone <this repo> squelch && cd squelch     # or copy the folder over
sudo ./install.sh
sudo nano /etc/squelch/squelch.toml             # node number, callsign, admin password
sudo systemctl start squelch
```

Open `http://<vm-ip>:8080`. The status dot will be gray until the node
starts streaming audio.

If the VM has a firewall, allow UDP 32001 (audio from the Pi) and TCP
8080 (web).

`install.sh` installs the system deps, builds the web UI (fetching Node.js if a
prebuilt `web/out` isn't present), creates the Python venv, and installs the
`squelch` systemd service. On an NVIDIA box it also drops in the CUDA service
units from `deploy/gpu/`.

## Configuration

All settings live in `/etc/squelch/squelch.toml` (created from
[`config.example.toml`](config.example.toml) on first install), and most are also
adjustable at runtime from the admin **Settings** panel. The essentials:

- `[node]` — your node number and callsign (shown in the header)
- `[transcribe]` — Whisper `model` and `device` (`cpu` or `cuda`)
- `[web]` — `admin_password` (bootstraps the first admin) and `site_name`

Optional features are config-gated and stay off until you enable them: voter RSSI
+ fuzzy geolocation (`[voter]`), live captions (`[captions]`), MDC forwarding
(`[mdc]`), DTMF (`[dtmf]`), and Say Again (`[sayagain]`).

## AllStar node setup (on the Pi, ASL3)

squelch receives audio the same way DVSwitch does: a private "pseudo
node" on the Pi whose radio interface is the USRP channel driver
pointed at the VM. That pseudo node then connects to your real node in
**monitor mode**, so it hears everything the node hears but can never
key it up.

1. **Enable the USRP channel driver.** In `/etc/asterisk/modules.conf`
   change `noload => chan_usrp.so` to:

   ```ini
   load => chan_usrp.so
   ```

2. **Add the pseudo node.** In `/etc/asterisk/rpt.conf` (replace
   `192.168.1.50` with your VM's IP and `546xx` with your real node
   number):

   ```ini
   [1999](node-main)
   rxchannel = USRP/192.168.1.50:32001:34001
                ; VM IP : port squelch listens on : local return port
   duplex = 0
   hangtime = 0
   telemdefault = 0
   startup_macro = *2546xx   ; monitor (receive-only) your real node
   ```

   > The **last** port is bound locally on the Pi. If another USRP
   > node (e.g. a DVSwitch bridge) already uses it, pick any free UDP
   > port there instead (`...:32001:34002`) — only the middle port has
   > to match what squelch listens on.

3. **Register the node number as private.** In the `[nodes]` stanza of
   `rpt.conf` add:

   ```ini
   1999 = radio@127.0.0.1:4569/1999,NONE
   ```

4. Restart Asterisk: `sudo astres.sh` (or `systemctl restart asterisk`).

Verify: `sudo asterisk -rx "rpt lstats 546xx"` should list 1999 as a
connected node (mode `Monitor`), and when someone keys the repeater the
squelch status dot turns red and a card appears within a few seconds of
unkey.

> `*2` = connect in monitor mode: node 1999 hears your node, but
> nothing 1999 does can reach the RF side. Combined with squelch never
> sending USRP audio back, transmit is impossible by construction.

## Testing without the radio

From the VM (or any machine that can reach it):

```sh
/opt/squelch/venv/bin/python /opt/squelch/app/tools/gen_mdc_wav.py /tmp/test.wav --unit 0x1234
/opt/squelch/venv/bin/python /opt/squelch/app/tools/send_wav.py /tmp/test.wav --host 127.0.0.1
```

A transmission card should appear with an `MDC PTT ID · 1234` badge.
You can also stream any speech WAV with `send_wav.py` to see
transcription and voice clustering work.

## Using the web UI

- **Live feed** — transmissions appear as they happen; the red banner
  shows while the node is receiving. Use ⏸ to pause the feed and the
  search box to search transcripts (full-text).
- **Admin login** — click **Login**, enter a username and password. The
  first login uses `admin` + the `admin_password` from the config; from
  then on you manage accounts in the UI.
- **Name a voice** — (admin) click the speaker chip on any
  transmission. *Rename voice* relabels that cluster everywhere;
  *Assign this TX* moves just that transmission to a person (and folds
  that voice sample into their profile — do this a few times to enroll
  someone the clusterer missed).
- **Whisper model** — (admin) dropdown in the header. Bigger models
  are more accurate and slower; `base.en` is a good CPU default,
  `small.en` if you have the cores. Takes effect on the next
  transmission.
- **Per-recording actions** — (admin) each card has a download button
  (saves an MP3; needs `ffmpeg` on the server, installed by
  `install.sh`) and a delete button. Each day divider has a button to
  bulk-delete that whole day.

  > **Note:** MP3 export shells out to `ffmpeg` per request and buffers
  > the whole encoded file in memory before sending it. That's fine for
  > occasional one-off downloads, but it isn't built for many
  > simultaneous exports on a small VM. It's admin-only, so bear it in
  > mind when handing out admin logins.

  A recording that's filed as case evidence can't be deleted or purged
  (the app returns a 409) — unfile it from the case first. This is what
  keeps a chain of custody intact.
- **Filters** — (any viewer) the funnel icon filters the feed by origin,
  speaker, MDC unit, date, and — for interference hunting — **audio with no
  recognized speech** (carriers, tones, jamming).
- **Cases** — (admin) the **Cases** button in the header opens investigative
  case management for documenting deliberate interference. Open a numbered case
  (e.g. `2026-001`), set a suspected operator and a summary, and file recordings
  as **evidence** straight from a transmission's ⋯ menu. Filed recordings are
  **exempt from audio retention** — they aren't pruned by age or the disk-space
  guard, and can't be purged, while a case references them. Each case keeps an
  **append-only activity log** (your notes plus system audit events) and exports
  as a **printable report** or a **ZIP** bundling the report with every attached
  recording. Admin/super only; public viewers never see Cases.
- **Settings panel** — (admin) the gear icon opens:
  - **Theme** — Night Mode (default) or Day Mode. This sets the site
    default; each viewer can also pick their own from the header.
  - **Logo** — upload a PNG/JPEG/SVG/WebP to replace the top-left mark.
  - **Footer text** — the "System maintained by…" line at the bottom.
  - **Change password** and **Users** — add or remove logins (you can't
    delete yourself or the last account).

## How voice ID works

Each transmission ≥1 s long gets a 256-dim voice embedding
(Resemblyzer/GE2E). It's compared by cosine similarity to every known
speaker's profile: above `match_threshold` it's tagged and refines
that profile; otherwise a new `Speaker N` is created. Radio audio is
narrow-band, so expect occasional misses — assigning a few
transmissions to the right person tightens their profile. If distinct
people get merged, raise `match_threshold`; if one person keeps
spawning new Speaker entries, lower it.

**Callsigns beat voice.** When the transcript contains a callsign
("this is W9ML" — direct, spelled out, or phonetic like "whiskey nine
mike lima"), squelch uses it: the transmission is assigned to the speaker
who owns that callsign (and their voice profile learns from it), and a
brand-new voice cluster that identifies itself gets named after its
callsign automatically. Legal ID is required every 10 minutes, so
speaker accuracy improves steadily on its own. Disable with
`use_callsigns = false`.

## Traffic origin (local RF vs connected node)

With the optional origin monitor running on the node, each transmission
gets a badge showing where its audio came from: the repeater's own
receiver ("<callsign> repeater") or a connected node ("via node 6453").
On the **node (Pi)**:

```sh
sudo cp /path/to/squelch/tools/source_monitor.py /usr/local/bin/squelch-source-monitor
sudo cp /path/to/squelch/deploy/squelch-source-monitor.service /etc/systemd/system/
sudo nano /etc/systemd/system/squelch-source-monitor.service
   # set --node to your node number, --url to the squelch VM,
   # --ignore to your squelch listener node (e.g. 1992)
sudo systemctl daemon-reload
sudo systemctl enable --now squelch-source-monitor
```

It talks to app_rpt over Asterisk's local AMI (credentials are read
from /etc/asterisk/manager.conf) — the same interface Allmon uses — and
reports key-up sources to squelch's `/api/source`. If badges don't
appear, run it by hand with `--debug` to see the raw AMI responses.

## MDC1200

There are two ways squelch gets MDC IDs, and which you want depends on
your repeater.

**1. Decode from the audio (default).** squelch runs the MDC decoder on
each transmission's audio. This works when the data burst survives into
the linked audio — but many repeaters gate the *pre*-burst out at the
receiver (squelch/COS timing) before it's ever repeated or linked, so
squelch's audio never contains it. Quick test: monitor the repeater's
**output**; if you can't hear the MDC blurp there, squelch won't get it
from audio either, and you want option 2.

**2. Forward app_rpt's own decodes (recommended for pre-burst ANI).**
app_rpt's MDC decoder taps the *raw* receiver audio, before that
gating, so it catches bursts that never make it into the repeated
audio. Enable it and forward the results to squelch:

On the **node (Pi)**:

1. Log decodes — add to the node's stanza in `/etc/asterisk/rpt.conf`:
   ```ini
   mdclog = /var/log/asterisk/mdc.log
   ```
   Restart Asterisk, key up an MDC radio, and confirm lines appear:
   ```sh
   cat /var/log/asterisk/mdc.log
   # 20260703002014 <your-node> I6402
   ```
2. Install the forwarder (standard-library Python, no pip needed):
   ```sh
   sudo cp /path/to/squelch/tools/mdc_forward.py /usr/local/bin/squelch-mdc-forward
   sudo cp /path/to/squelch/deploy/squelch-mdc-forward.service /etc/systemd/system/
   sudo sed -i 's#<vm-ip>#192.168.1.50#' \
       /etc/systemd/system/squelch-mdc-forward.service
   sudo systemctl daemon-reload
   sudo systemctl enable --now squelch-mdc-forward
   ```

That's it — every decoded ID is POSTed to squelch's `/api/mdc` and
attached to the matching transmission by time. It works for **every
user on the machine**, not just radios you control. No clock sync
between the node and the VM is needed (squelch times events by arrival,
and the forwarder only ships new bursts).

If squelch is reachable off your LAN, set a `[mdc] forward_token` in
`squelch.toml` and the matching `SQUELCH_MDC_TOKEN` in the service file so
only your node can post IDs.

Badge shows the burst type (PTT ID / Emergency / Status / Call) and the
unit ID as app_rpt reports it. Audio-decoded bursts (option 1) also
show the opcode and hex unit ID.

## Troubleshooting

| Symptom | Check |
|---|---|
| Status pill stays "listening" but no cards; `/api/status` shows `last_frame: null` while `tcpdump` sees packets | Firewall dropping UDP 32001. tcpdump taps before the firewall, the socket is after it. With ufw: `sudo ufw allow from <lan>/24 to any port 32001 proto udp`. |
| Status dot stays gray | Pi can reach VM? `sudo tcpdump -i any udp port 32001` on the VM while keying the node. Check `modules.conf` and the rxchannel IP/port. |
| Cards appear but no transcript | `journalctl -u squelch -f` — first transcription downloads the model (needs internet once). `whisper_available: false` in `/api/status` means faster-whisper didn't install. |
| No speaker labels | Transmissions must be ≥ `min_embed_ms` (default 1 s). `speaker_id_available: false` means resemblyzer/torch didn't install. |
| No MDC decodes | Confirm bursts survive to the stream: generate a test WAV (above) — if that decodes, the repeater is muting MDC on air. |
| Slow / backlog ("processing N") | Use a smaller Whisper model or give the VM more cores. |

## Development

```sh
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt pytest httpx
pytest
python -m squelch -c config.example.toml   # needs data_dir you can write to
```

## Contributing

It's GPL-2.0 — fork away. Issues and pull requests are welcome. Two ground rules:
keep the **receive-only guarantee** intact (Squelch must never be able to key the
node), and run the test suite (`pytest`) before opening a PR.

## License

GPL-2.0. The MDC1200 modem is a Python port of
[Matthew Kaufman's MDC Encoder/Decoder Library](https://github.com/atmatthewat/mdc-encode-decode)
(GPLv2), which makes the project as a whole GPLv2. 73!
