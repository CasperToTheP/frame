# Frame

Software for a wall-mounted digital art frame built from a Raspberry Pi and an HDMI monitor.

You plug it in and the artwork you chose last time starts fullscreen and loops forever,
with sound through the monitor's speakers. Nothing else is ever on screen: no desktop,
no console, no cursor, no player controls. To change the artwork, open
`http://frame.local:8080` on a phone or laptop on the same Wi-Fi. From there you can upload
videos, pick what plays, and set volume, mute and screen rotation.

It's built to work like an appliance. It survives power cuts, restarts the player if it
crashes, keeps playing when Wi-Fi drops, and needs no internet once the artwork is uploaded.

> **Security:** the web UI has no password. It's meant **only for a trusted home network**.
> Never forward port 8080 on your router or expose it to the internet.

---

## Contents

- [How it works](#how-it-works)
- [Hardware](#hardware)
- [Prepare the Raspberry Pi](#prepare-the-raspberry-pi)
- [Install Frame](#install-frame)
- [Using it](#using-it)
- [Preparing artwork](#preparing-artwork)
- [Troubleshooting](#troubleshooting)
- [Development](#development)
- [Future ideas](#future-ideas)

## How it works

```
 phone / laptop ──HTTP :8080──▶ frame-web (Flask + waitress, user "frame")
                                   │  saves settings ──▶ /var/lib/frame/state.json
                                   │  JSON IPC
                                   ▼
                          /run/frame/mpv.sock
                                   ▲
 frame-player (supervisor) ──runs──┘ mpv ──DRM/KMS──▶ HDMI display + HDMI audio
       reads state.json at start, restarts mpv on crash / hang / HDMI replug
```

- **frame-player.service** keeps one fullscreen `mpv` running. mpv draws directly to the
  display through DRM/KMS, so no desktop is needed. When mpv starts, it gets the saved
  artwork, volume and rotation. If mpv exits, hangs or can't open the display, it is
  restarted with exponential backoff (1 s up to 60 s). The player also waits for a display
  to be connected, and restarts mpv when the HDMI cable or monitor is reconnected.
- **frame-web.service** serves the UI and the JSON API. It changes playback live through
  mpv's IPC socket, so mpv is never restarted just to switch artwork. Every change is saved
  to `state.json` first, so it survives reboots and player restarts.
- Playback doesn't depend on the web service. If the web service crashes or Wi-Fi drops,
  the artwork keeps playing.

## Hardware

| Part | Used here |
|---|---|
| Computer | Raspberry Pi 4 Model B, **1 GB RAM** |
| Storage | 64 GB microSD card |
| Power | Official Raspberry Pi 5.1 V / 3 A USB-C power supply |
| Network | Built-in Wi-Fi |
| Display | DUTZO C16-I60FC 15.6" 1920×1080 IPS portable monitor, Mini-HDMI input, built-in stereo speakers, own power supply |
| Cable | Micro-HDMI (Pi) → Mini-HDMI (monitor) |
| Enclosure | Custom wooden wall frame; Pi and monitor mounted behind/inside |

Any standard HDMI display with speakers works too. Playback is designed for 1920×1080 but
uses whatever mode the display prefers.

**Tips:**
- Use the Pi's **HDMI0** port (the one next to the USB-C power socket). Frame finds the
  connected port automatically, but HDMI0 is the standard choice.
- Use the official power supply. Under-voltage causes stutter and SD card corruption.
  `frame-doctor` reports it.
- The Pi 4 runs warm inside a closed frame. Leave some airflow or add a small heatsink.

## Prepare the Raspberry Pi

1. Install **Raspberry Pi Imager** on your computer and choose:
   - Device: *Raspberry Pi 4*
   - OS: *Raspberry Pi OS (other)* → **Raspberry Pi OS Lite (64-bit)**. Frame doesn't need the desktop.
   - Storage: your microSD card
2. Click **Edit settings** when asked about OS customisation, and set:
   - **Hostname:** `frame`. This makes the UI available at `http://frame.local:8080`.
   - **Username and password** for yourself, for SSH.
   - **Wireless LAN:** your Wi-Fi name, password and country.
   - **Services** tab: **Enable SSH** (password authentication is fine).
3. Write the card, put it in the Pi, connect the monitor (HDMI0) and power it on. The first
   boot takes a few minutes.
4. From your computer, connect over SSH:
   ```bash
   ssh <your-username>@frame.local
   ```
   If `frame.local` doesn't resolve, find the Pi's IP address in your router's device list
   and use `ssh <your-username>@<ip-address>` instead.

## Install Frame

On the Pi, over SSH:

```bash
sudo apt update && sudo apt install -y git
git clone https://github.com/CasperToTheP/frame.git
cd frame
sudo ./scripts/install.sh
sudo reboot
```

After the reboot, open **http://frame.local:8080** on your phone. You can also use
**http://\<pi-ip-address\>:8080**; the installer prints the address, and `hostname -I`
shows it too.

What the installer does (it's safe to run again, and it never deletes media or settings):

- installs `mpv`, Python venv support, Avahi (mDNS) and ALSA tools with apt
- creates a system user `frame` in the `video`, `render` and `audio` groups, which give
  access to the display, GPU and sound. Neither service runs as root.
- installs the app into `/opt/frame` with its own Python virtualenv (`/opt/frame/venv`)
- creates `/var/lib/frame/media` (artwork) and `/var/lib/frame/state.json` (settings)
- installs and enables `frame-player.service` and `frame-web.service`
- advertises the UI over mDNS. If the hostname is still the default `raspberrypi`, it
  renames it to `frame`. Use `--hostname NAME` to choose another name.
- turns off Wi-Fi power saving, which otherwise makes the UI slow to answer
- sets up a quiet boot: no rainbow splash, no boot text or blinking cursor, and no login
  prompt on the HDMI screen. SSH still works. The original boot files are saved as
  `cmdline.txt.frame-backup` and `config.txt.frame-backup` in `/boot/firmware`. To skip
  this step, run with `--no-boot-config`.

Optional settings go in `/etc/default/frame`: port, upload size limit and log level.

## Using it

### Open the web UI
`http://frame.local:8080` (or `http://<pi-ip>:8080`). Tip: add it to your phone's home screen.

### Upload artwork
In **Upload artwork**, choose a file and tap **Upload**. A progress bar shows while it
uploads. Uploading doesn't change what's playing unless you tick **Play after upload**.
Supported formats: `.mp4` (recommended), `.mkv`, `.webm`, `.mov`, `.m4v`, `.gif`, `.jpg`, `.png`.
The default size limit is 4 GB, and an upload is refused if it would leave less than
256 MB free on the SD card.

### Select artwork
Tap **Play** next to a file in the **Library**. The selection is saved and still applies
after a reboot or power cut.

### Volume, mute and pause
Use the slider and buttons under **Now playing**. Volume and mute are saved; pause isn't,
because after a reboot the frame should always be playing. **Black screen** stops playback
and leaves the screen black.

### Rotation and fit
Under **Display**:
- **Rotation:** 0°, 90°, 180° or 270°, for mounting the frame portrait or upside down.
  The change applies instantly.
- **Fit:** *Fit* shows the whole artwork with black bars (the default). *Fill* crops to
  fill the screen. *Stretch* distorts the image to fill the screen.
- **Advanced:** pick the audio output and the hardware decoding mode, and see which
  decoder is in use.

### Delete artwork
Tap **Delete**. Deleting the file that's playing asks you to confirm, then shows a black
screen.

### Reboot / shut down
```bash
ssh <user>@frame.local
sudo reboot               # restart
sudo shutdown -h now      # before unplugging, if you can (unplugging also works)
```

### Update the software
```bash
cd ~/frame
git pull
sudo ./scripts/install.sh
```
Your media and settings are kept. The services restart automatically.

### Uninstall
```bash
sudo ./scripts/uninstall.sh          # keeps /var/lib/frame (media + settings)
sudo ./scripts/uninstall.sh --purge  # deletes everything
```

### API
The UI uses a small JSON API that you can also script against:

| Method & path | Body | Does |
|---|---|---|
| `GET /api/status` | | current artwork, playback state, volume, mute, rotation, player health |
| `GET /api/media` | | list of files (+ free disk space) |
| `POST /api/media` | multipart `file` (+ optional `play=1`) | upload |
| `DELETE /api/media/<name>[?force=1]` | | delete (`force` is needed for the playing file) |
| `POST /api/play` | `{"filename": "art.mp4"}` | select artwork |
| `POST /api/stop` | | black screen |
| `POST /api/pause` / `POST /api/resume` | | pause / resume |
| `POST /api/volume` | `{"volume": 60}` | volume 0–100 |
| `POST /api/mute` | `{"muted": true}` or `{}` to toggle | mute |
| `POST /api/settings` | `{"rotation": 90, "fit": "fill", "audio_device": "auto", "hwdec": "auto-safe"}` | display/audio settings |
| `GET /api/audio-devices` | | audio outputs mpv can see |

Example: `curl -X POST -H 'Content-Type: application/json' -d '{"volume":40}' http://frame.local:8080/api/volume`

## Preparing artwork

### Recommended format

The Raspberry Pi 4 decodes **H.264** in hardware up to 1080p60. Use:

| | Setting |
|---|---|
| Container | MP4 |
| Video | H.264 (High profile, level 4.1 or lower), 8-bit `yuv420p` |
| Resolution | 1920×1080 or smaller |
| Frame rate | 30 fps (60 fps works; 30 is easier on the Pi) |
| Bitrate | about 8–15 Mbit/s for 1080p is plenty |
| Audio | AAC, stereo, 48 kHz, 128–192 kbit/s |

Avoid HEVC/H.265 above 1080p, 10-bit video, 4K, and AV1. They play badly or not at all on a
Pi 4. Frame doesn't transcode for you; convert artwork on your computer with
[ffmpeg](https://ffmpeg.org/).

### Convert anything to a frame-friendly MP4

```bash
ffmpeg -i input.mov \
  -vf "scale=1920:1080:force_original_aspect_ratio=decrease:force_divisible_by=2,fps=30" \
  -c:v libx264 -profile:v high -level:v 4.1 -pix_fmt yuv420p -preset slow -crf 20 \
  -g 60 -c:a aac -b:a 192k -ar 48000 -ac 2 \
  -movflags +faststart \
  artwork.mp4
```

- `scale=...decrease`: fits the video inside 1920×1080 without distorting it.
- `-crf 20`: high quality. Use a lower number for better quality and bigger files.
- If the source has no audio, add `-an`. If you want silence, generate it:
  `-f lavfi -i anullsrc=r=48000:cl=stereo -shortest`.

### Portrait frames

If the frame hangs in portrait, the most reliable option is to rotate the artwork
**in the file** so it's a normal 1920×1080 landscape video, then leave Rotation at 0°:

```bash
# portrait source (e.g. 1080x1920) -> landscape file for a frame mounted rotated 90° clockwise
ffmpeg -i portrait.mp4 -vf "transpose=2,scale=1920:1080:force_original_aspect_ratio=decrease:force_divisible_by=2" \
  -c:v libx264 -profile:v high -level:v 4.1 -pix_fmt yuv420p -crf 20 -c:a aac -b:a 192k -ar 48000 artwork-portrait.mp4
```

(Use `transpose=1` instead if it comes out upside down.) The other option is to upload
the portrait file as it is and set **Rotation** to 90° or 270°. That's easier but may be
decoded in software. Check **Display → Advanced → Decoder** in the UI.

### Making genuinely seamless loops

mpv loops a file by jumping back to its start. With a good file the loop is nearly
invisible, but the content itself has to loop:

1. **Edit it as a loop.** The last frame should flow into the first, for example by
   crossfading the end of the clip into its start in your editor.
2. **Keep audio and video exactly the same length.** Cut both to the same duration
   (`-shortest` helps). A difference of a few milliseconds creates a gap or a click.
3. **Make the audio loop too.** Edit the sound so its end flows into its start, or add
   2–5 ms fade-in/out (`-af "afade=t=in:d=0.005,afade=t=out:st=<len-0.005>:d=0.005"`) so
   the seam doesn't click.
4. **Use a whole number of frames.** For example 10.000 s at 30 fps = 300 frames.
5. **Small is smooth.** mpv keeps up to 32 MB of already-played data in RAM, so short
   files usually loop back without touching the SD card.
6. **Longer loops have fewer seams.** A 2-minute loop shows a seam 30 times an hour; a
   2-second loop shows it 1,800 times.

## Troubleshooting

First, run the diagnostic script. It's read-only, so it's safe to run any time:

```bash
sudo frame-doctor            # or: sudo ./scripts/doctor.sh from the repo
```

It shows the Pi model, OS, mpv version, connected displays, audio devices, service states,
player IPC health and the current state, disk space, IP addresses, temperature,
under-voltage flags and recent errors.

### Logs
```bash
journalctl -u frame-player -u frame-web -f          # follow live
journalctl -u frame-player -b                       # player, since boot
journalctl -u frame-web --since "10 min ago"
```

### No picture
- Is the monitor on and set to the HDMI input? `frame-doctor` should show
  `card?-HDMI-A-1: connected`. If nothing is connected, the player logs
  `no HDMI display connected; waiting for one` and starts as soon as a display appears.
- `/boot/firmware/config.txt` must contain `dtoverlay=vc4-kms-v3d`. This is the Raspberry
  Pi OS default; `frame-doctor` checks it.
- Check `journalctl -u frame-player -b` for `mpv health check failed` or
  `Failed to create KMS`.
- If the monitor powers up **after** the Pi and stays black, unplug and replug HDMI. The
  player restarts mpv on a hotplug. If your monitor never signals hotplug, add
  `video=HDMI-A-1:1920x1080@60D` to the single line in `/boot/firmware/cmdline.txt` and
  reboot. That forces the output on.
- Stutter or a black picture with sound: try **Display → Advanced → Hardware decoding**
  set to `drm` or `v4l2m2m-copy`, or `no` to rule decoding out. Re-encode the file with the
  recommended settings above.

### No HDMI sound
- Check the monitor's own volume and that it isn't muted. Many portable monitors start at
  a low volume.
- In the UI, check **Mute** and **Volume**.
- `frame-doctor` → **Audio** should list `vc4hdmi0` (HDMI0) or `vc4hdmi1` (HDMI1).
  **Automatic** audio output uses the port the display is connected to.
- In **Display → Advanced → Audio output**, try the other HDMI entries listed by mpv,
  e.g. `alsa/sysdefault:CARD=vc4hdmi0`.
- Test outside Frame: `sudo systemctl stop frame-player`, then
  `speaker-test -D hdmi:CARD=vc4hdmi0,DEV=0 -c 2 -t sine -l 1`, then
  `sudo systemctl start frame-player`.
- If the file has no audio track (e.g. a GIF), there's nothing to play.

### Player doesn't start
```bash
systemctl status frame-player
journalctl -u frame-player -b --no-pager | tail -50
```
- `could not start mpv`: reinstall with `sudo ./scripts/install.sh`.
- `Permission denied` on `/dev/dri` or `/dev/snd`: check `id frame` includes
  `video render audio`, then re-run the installer.
- The service retries forever with backoff (up to 60 s between attempts), so once the
  cause is fixed it recovers by itself.

### `frame.local` doesn't resolve
- Use the IP address instead: `http://<pi-ip>:8080`. `hostname -I` on the Pi shows it, or
  check your router's device list.
- Windows needs Bonjour/mDNS support. Recent Windows 10/11 has it; otherwise install
  iTunes or Bonjour Print Services. Android supports `.local` in Chrome on recent versions;
  older Android may need the IP.
- Some routers block multicast between Wi-Fi clients ("AP/client isolation"). Turn it off,
  or use the IP and give the Pi a fixed DHCP reservation in your router.
- Check `systemctl status avahi-daemon` and `hostname` on the Pi.

### Web UI unreachable but the artwork plays
Playback doesn't need the web service. Check `systemctl status frame-web`, then try
`curl http://127.0.0.1:8080/api/status` on the Pi. If that works but your phone can't
connect, it's a network problem: Wi-Fi, client isolation or the IP address.

## Development

Requirements: Python 3.11+. The code runs on Linux, macOS and Windows for development;
only actual playback needs a Pi.

```bash
python -m venv .venv
. .venv/bin/activate              # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
pytest                            # unit + API tests (no Pi needed)
ruff check .                      # lint
```

`tests/test_mpv_integration.py` drives a **real mpv** (with null video and audio
outputs). It runs when `mpv` and `ffmpeg` are installed (Linux/macOS) and is skipped
otherwise.

Run the web UI locally against a scratch data directory (the player shows as offline):

```bash
FRAME_DATA_DIR=dev-data FRAME_RUN_DIR=dev-data/run FRAME_PORT=8090 python -m frame.web
```

Repository layout:

```
frame/
  config.py          paths & settings from environment variables
  state.py           state.json load/validate/atomic save
  media.py           media library, filename sanitising, upload storage
  display.py         HDMI connector + HDMI audio detection (sysfs)
  player.py          mpv command line + JSON IPC client
  player_service.py  frame-player: mpv supervisor
  controller.py      actions behind the API (play, volume, delete, ...)
  web.py             frame-web: Flask app + waitress entry point
  templates/, static/  the UI (server-rendered HTML, CSS, vanilla JS)
scripts/             install.sh, uninstall.sh, doctor.sh
systemd/             service units, tmpfiles and Avahi config
tests/
```

See [CLAUDE.md](CLAUDE.md) for design notes and constraints.

## Future ideas

These are deliberately not in version 1:

- **Scheduled screen on/off**, e.g. dark at night without shutting down the Pi. The
  planned approach: the player service would take a schedule from `state.json` and
  turn the display off and on. The hardware-specific code would go in `frame/display.py`.
- Optional password for the web UI. `check_access()` in `frame/web.py` is the single place
  to add it.
- Playlists or rotating artwork, thumbnails, Home Assistant integration.
