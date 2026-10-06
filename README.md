# Frame

Software for a wall-mounted digital art frame built from a Raspberry Pi and an HDMI monitor.

You plug it in and your artwork starts fullscreen and loops forever, with sound through
the monitor's speakers. Show one piece, or a playlist that changes every few minutes with
a soft fade through black. Artwork can be a video, a GIF or an image (NFTs included), and
the sound can come from the artwork itself or from your own playlist of music. Nothing else is ever on screen: no desktop,
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
- [Supported files](#supported-files)
- [Preparing artwork](#preparing-artwork)
- [NFTs](#nfts)
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
       │                     └─runs── mpv (audio only) ──▶ HDMI audio, for a separate soundtrack
       reads state.json at start, restarts both on crash / hang / HDMI replug
```

- **frame-player.service** keeps one fullscreen `mpv` running. mpv draws directly to the
  display through DRM/KMS, so no desktop is needed. When mpv starts, it gets the saved
  artwork, volume and rotation. If mpv exits, hangs or can't open the display, it is
  restarted with exponential backoff (1 s up to 60 s). The player also waits for a display
  to be connected, and restarts mpv when the HDMI cable or monitor is reconnected.
  A **freeze watchdog** restarts both players if a video's picture stops moving for 15 s
  while it isn't paused. If mpv gets stuck in the kernel because the Pi's graphics
  firmware has hung, the Pi **reboots itself** (at most 3 times in 6 hours). In both
  cases the file that was playing is logged and left out of the playlist until you next
  change the playlist (unless it's the only artwork).
- **frame-web.service** serves the UI and the JSON API. It changes playback live through
  mpv's IPC socket, so mpv is never restarted just to switch artwork. Every change is saved
  to `state.json` first, so it survives reboots and player restarts.
- The player service also runs the **playlists**: it moves to the next artwork or track on
  a timer and fades between them, so playlists keep running without the web UI.
- A **second, audio-only mpv** plays a separate soundtrack when you choose one. It loops on
  its own, independently of the artwork. Only one player uses the HDMI audio at a time:
  with a soundtrack chosen, the artwork's own sound is switched off.
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

First time setting up a Pi? [docs/pi-setup.md](docs/pi-setup.md) walks through every step
in more detail, and ends with a checklist for testing the frame on real hardware.

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

Optional settings go in `/etc/default/frame`: port, upload size limit, log level and the
hotspot name and password (see [Without home Wi-Fi](#without-home-wi-fi)).

## Using it

### Open the web UI
`http://frame.local:8080` (or `http://<pi-ip>:8080`). Tip: add it to your phone's home screen.

### Upload
In **Upload**, choose one or more files and tap **Upload**. A progress bar shows while it
uploads. With **Add to playlist / sound** ticked (the default), visuals are added to the
end of the playlist and audio files to the sound list. See
[Supported files](#supported-files). The default size limit is 4 GB, and an upload is
refused if it would leave less than 256 MB free on the SD card.

Files that are likely too heavy for a Pi 4 are accepted, but marked with a ⚠ warning in
**Library** saying why: larger than 1920×1080, H.264 above level 4.2, 10-bit or 4:2:2
video, AV1 or VP9, or a GIF over 50 MB. Convert them as described in
[Preparing artwork](#preparing-artwork).

### Playlist
The **Playlist** is the artwork the frame shows, in order. Everything is saved and
continues after a reboot or power cut.
- **Add** next to a file in the **Library** puts it at the end. **↑ ↓** reorder, **✕**
  removes from the playlist (the file stays in the Library).
- **Play** next to a file shows only that one, looping forever. This replaces the
  playlist.
- **Change every**: how long each item stays, from 15 seconds to 1 hour. *Full length*
  plays each video or GIF once through; images then stay for 1 minute. Videos shorter than
  the interval loop until it's time to move on.
- **Fade between items**: the picture fades to black and back (and so does the
  artwork's own sound). *Off* cuts straight over.
- **Shuffle** plays the items in random order, never the same one twice in a row.
- **Next** under **Now playing** skips ahead now. The countdown shows when the next change
  is due.

### Sound
The **Sound** list chooses what you hear:
- **Empty** (the default): each artwork's own sound, the audio inside the video file.
  GIFs and images are silent.
- **Audio files**: they play instead, as their own playlist, independent of the artwork.
  A 3-second GIF can play over a 4-minute track, and changing artwork doesn't interrupt
  the music. **Change track** at the end of each track or on a timer, with **Shuffle** and
  **Next track**; tracks fade out and in. **Use artwork's own sound** empties the list.
  **Play** next to an audio file plays only that one.

The sound only plays while artwork is on screen: **Black screen** silences it too.

### Volume, mute and pause
Use the slider and buttons under **Now playing**. Volume and mute are saved; pause isn't,
because after a reboot the frame should always be playing. **Black screen** stops playback
and leaves the screen black without forgetting the playlist; **Start** brings it back.
Pause also pauses the playlist timers.

### Rotation and fit
Under **Display**:
- **Rotation:** 0°, 90°, 180° or 270°, for mounting the frame portrait or upside down.
  The change applies instantly.
- **Fit:** *Fit* shows the whole artwork with black bars (the default). *Fill* crops to
  fill the screen. *Stretch* distorts the image to fill the screen.
- **Scaling:** *Smooth* suits photos and video. *Sharp (pixel art)* keeps every pixel a
  crisp square when small artwork is enlarged, for pixel-art NFTs and retro GIFs.
- **Advanced:** pick the audio output and the hardware decoding mode, and see which
  decoder is in use.

### Without home Wi-Fi
The artwork always plays without any network. Only the web UI needs a connection. If
the frame can't reach your home Wi-Fi (weak signal, router off, a new home, or no Wi-Fi
saved at all), it starts **its own Wi-Fi network** after about 1.5 minutes:

1. On your phone, join the Wi-Fi **Frame**. The password is printed at the end of the
   installer, shown in the web UI under **Display → Advanced**, and stored in
   `/etc/default/frame`.
2. Open **http://10.42.0.1:8080**.

Your phone may say the network has no internet; stay connected anyway. While nobody is
connected to the hotspot, the frame tries the home Wi-Fi again every 10 minutes, and goes
back to it as soon as it's in reach. The Pi's Wi-Fi can only do one of the two at a time.

To run the frame **without home Wi-Fi for good**, forget the home network on the Pi
(`sudo nmcli connection delete "<your network>"`). It then always uses its own hotspot.
To change the hotspot's name or password, edit `FRAME_HOTSPOT_SSID` and
`FRAME_HOTSPOT_PASSWORD` in `/etc/default/frame` (8-63 characters) and re-run the
installer.

### Delete artwork
Tap the **bin** next to a file. If it's in the playlist or sound list, you're asked to
confirm; it's removed from the list and the frame moves on to the next item.

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
| `GET /api/status` | | what's playing (`now`: position, count, next change), playlists, settings, player health |
| `GET /api/media` | | list of files (+ free disk space) |
| `POST /api/media` | multipart `file` (one or more; + optional `add=1` or `play=1`) | upload |
| `DELETE /api/media/<name>[?force=1]` | | delete (`force` is needed for files in the playlist or sound list) |
| `POST /api/play` | `{"filename": "art.mp4"}` | show only this artwork |
| `POST /api/add` | `{"filename": "art.mp4"}` | add to the playlist (or, for audio, the sound list) |
| `POST /api/playlist` | any of `{"items": [...], "interval": 300, "shuffle": false}` | the visual playlist; `interval` in seconds, `0` = full length |
| `POST /api/sounds` | any of `{"items": [...], "interval": 0, "shuffle": false}` | the sound list; `[]` = artwork's own sound, `interval` `0` = whole track |
| `POST /api/soundtrack` | `{"filename": "song.mp3"}` or `{"filename": null}` | play only this audio file / back to the artwork's own sound |
| `POST /api/next` | `{"which": "visual"}` or `{"which": "sound"}` | skip to the next item now |
| `POST /api/stop` / `POST /api/start` | | black screen (keeps the playlist) / start again |
| `POST /api/pause` / `POST /api/resume` | | pause / resume |
| `POST /api/volume` | `{"volume": 60}` | volume 0–100 |
| `POST /api/mute` | `{"muted": true}` or `{}` to toggle | mute |
| `POST /api/settings` | `{"rotation": 90, "fit": "fill", "scaling": "sharp", "fade": 1, "audio_device": "auto", "hwdec": "auto-safe"}` | display/audio settings (`fade` in seconds, `0` = off) |
| `GET /api/audio-devices` | | audio outputs mpv can see |

Example: `curl -X POST -H 'Content-Type: application/json' -d '{"volume":40}' http://frame.local:8080/api/volume`

## Supported files

| Kind | Formats | Notes |
|---|---|---|
| Video | `.mp4` (recommended), `.m4v`, `.mov`, `.mkv`, `.webm` | Loops forever. H.264 is decoded in hardware; see below. |
| Animation | `.gif` | Loops forever, including transparent GIFs. |
| Image | `.jpg`, `.png`, `.webp`, `.bmp`, `.tif`/`.tiff` | Stays on screen. Transparency is shown on black. |
| Vector image | `.svg` | Converted to a sharp PNG (1920 px on the longest side) when uploaded. |
| Sound | `.mp3`, `.m4a`, `.aac`, `.wav`, `.flac`, `.ogg`, `.opus` | Chosen under **Sound**; loops on its own. |

Not supported: **animated WebP** (the Pi's video library can't decode it; convert it, see
[NFTs](#nfts)), HEIC/AVIF photos, and HTML or 3D (`.glb`) artwork, which needs a web
browser or a 3D engine the 1 GB Pi doesn't run.

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
Pi 4: they're decoded in software with large buffers, which can freeze the picture or run
the 1 GB Pi out of memory. The web UI warns about such files. Frame doesn't transcode for
you; convert artwork on your computer with [ffmpeg](https://ffmpeg.org/).

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

## NFTs

Frame plays the artwork **file** of an NFT. It doesn't connect to a wallet or the
internet: download the files once, upload them, and the frame works offline.

**Getting the original file.** Marketplace previews are often small or re-compressed. For
the best quality, open the NFT on its marketplace or block explorer, find the original
media link (often `ipfs://…` or an `arweave.net` link), and download that file. An
`ipfs://<CID>/<file>` link can be opened in a browser as
`https://ipfs.io/ipfs/<CID>/<file>`.

**What works as-is:** MP4/MOV/WebM videos, GIFs, PNG/JPG/still WebP images, and on-chain
SVG art (converted on upload). Transparent backgrounds show as black.

**Pixel art** (punks, 8-bit art and so on): set **Display → Scaling** to
**Sharp (pixel art)**, otherwise enlarging a 24×24 image blurs it.

**Animated WebP:** convert it to MP4 on your computer. ffmpeg can't read animated WebP
before version 8, so use the `webp` tools to unpack it first:

```bash
# Debian/Ubuntu: sudo apt install webp   ·   macOS: brew install webp
mkdir frames
anim_dump -folder frames -prefix f_ art.webp            # one PNG per frame
ffmpeg -framerate 12 -i frames/f_%04d.png \
  -vf "scale=trunc(iw/2)*2:trunc(ih/2)*2:flags=neighbor" \
  -c:v libx264 -pix_fmt yuv420p -crf 18 art.mp4
```

Set `-framerate` to the animation's speed. Alternatively, convert it to a GIF with any
online "WebP to GIF" converter.

**Music NFTs, or art with a separate audio file:** upload the visual and the audio file
separately, then choose the audio under **Sound**.

**HTML / generative (JavaScript) and 3D NFTs** can't run on the frame. Record them as a
video on your computer (most marketplaces and many projects offer an MP4 render), then
upload that.

## Troubleshooting

First, run the diagnostic script. It's read-only, so it's safe to run any time:

```bash
sudo frame-doctor            # or: sudo ./scripts/doctor.sh from the repo
```

It shows the Pi model, OS, mpv version, connected displays, audio devices, service states,
player IPC health and the current state, disk space, IP addresses, temperature,
under-voltage flags, graphics firmware hangs, out-of-memory kills and recent errors.

### Logs
```bash
journalctl -u frame-player -u frame-web -f          # follow live
journalctl -u frame-player -b                       # player, since boot
journalctl -u frame-player -b -1                    # player, the boot before
journalctl -u frame-web --since "10 min ago"
```
The installer keeps logs across reboots (Raspberry Pi OS keeps them in RAM only), capped
at 32 MB.

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

### Picture frozen, sound still playing
The player restarts itself after 15 s of frozen picture, and reboots the Pi if the
graphics firmware has hung, so this should clear up within about a minute. To find out
which file caused it:

```bash
journalctl -u frame-player -b -1 | grep -E "frozen|stuck|rebooting"   # -b -1: the boot before
sudo cat /var/lib/frame/hang.json
```

Almost always the file is too heavy for the Pi (see the ⚠ in **Library**). Convert it with
the command in [Preparing artwork](#preparing-artwork). If it keeps happening,
`frame-doctor` shows whether the kernel ran out of memory.

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
- If the file has no audio track (e.g. a GIF), there's nothing to play. Choose an audio
  file under **Sound** instead.
- If HDMI audio isn't ready when a file starts (e.g. the monitor is still waking up), mpv
  plays on without sound. The player notices (`no audio output` in the log) and reopens
  the audio output in the background after 30 s, then less often, without interrupting
  the picture.
- If a separate **Sound** is chosen, the artwork's own sound is off on purpose. `frame-doctor`
  shows both players (artwork and soundtrack) and which one has the audio output open.

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
If the Wi-Fi signal is the problem, look for the frame's own Wi-Fi **Frame** (see
[Without home Wi-Fi](#without-home-wi-fi)). `frame-doctor` shows the signal strength.
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
  player.py          mpv command lines (artwork + soundtrack) + JSON IPC client
  player_service.py  frame-player: supervises both mpv processes
  director.py        playlists: what plays when, and the fades in between
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
- Thumbnails, Home Assistant integration.
- Importing NFTs straight from a wallet address.

## Licence

[MIT](LICENSE)
