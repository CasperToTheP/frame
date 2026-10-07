# CLAUDE.md

Notes for working on this repository. Read this first; the README covers end-user setup.

## Purpose

Software for a wall-mounted digital art frame. A Raspberry Pi drives an HDMI monitor that
loops one video artwork fullscreen with sound, forever. It's managed from a phone through a
small web UI on the home LAN. It must behave like an **appliance**: boots straight into the
artwork, recovers by itself, never shows a desktop, console, cursor or player UI, and keeps
working offline.

## Hardware constraints

- Raspberry Pi 4 Model B with **1 GB RAM**. Every dependency and process has to justify
  its memory.
- Raspberry Pi OS **Lite** 64-bit (Bookworm: Python 3.11, mpv 0.35; Trixie: Python 3.13,
  mpv 0.40). No desktop, X11 or Wayland.
- Display: 1920×1080 HDMI (DUTZO C16-I60FC portable monitor with speakers), but any HDMI
  display must work. HDMI audio only.
- microSD storage: avoid needless writes, and write state atomically (power can be cut
  at any time).
- Wi-Fi only. Playback must never depend on the network.

## Architecture

Two main systemd services, both running as the unprivileged system user `frame`, plus a
small root service for the Wi-Fi fallback:

| Service | Module | Role |
|---|---|---|
| `frame-player.service` | `frame/player_service.py` | Supervisor. Starts the artwork mpv and an audio-only "soundtrack" mpv with the saved state, and treats them as a pair: if either exits, hangs (IPC health check), the artwork mpv has no video output (`vo-configured` false) or a video's `time-pos` stops moving for 15 s while not paused (freeze watchdog), both restart with backoff. Waits for an HDMI display and restarts both on HDMI hotplug. Writes `/run/frame/player.json`. Never imports Flask. |
| `frame-web.service` | `frame/web.py` (+ `controller.py`) | Flask app served by waitress on :8080. Server-rendered HTML + vanilla JS. Saves what to play in `state.json`; changes live settings (volume, pause, rotation...) over mpv JSON IPC. |
| `frame-netwatch.service` | `frame/netwatch.py` | Runs as root. When the home Wi-Fi can't be reached for 90 s, brings up the NetworkManager connection `frame-hotspot` (made by the installer, autoconnect off) so a phone can reach the UI at `http://10.42.0.1:8080`. While no phone is connected, hands Wi-Fi back to NetworkManager every 10 min to try the home network. Only runs `nmcli` and `iw`. Writes `/run/frame/network.json`. |

Key rules:

- **Persist first, then tell mpv.** The controller writes `state.json` and then (for live
  settings) sends the IPC command. If mpv is down, the player service applies the saved state when it
  restarts mpv. Only the web service writes `state.json`; the player only reads it.
- **The Director decides what plays** (`frame/director.py`, run by the supervisor every
  0.5 s). It is the only code that loads files into either mpv: it steps through the
  visual playlist and the sound list on their own clocks, applies changes the web UI
  saved (it watches `state.json`'s mtime), and handles "next" requests, which the web
  service makes by creating `/run/frame/next-visual` or `next-sound`. It publishes
  what's playing in `player.json` (`playlist` key) for the UI. A broken file is skipped.
- **Fades** go through black with mpv's `brightness` property (-100 = black) plus volume
  ramps; `brightness` persists across `loadfile`, so the next file starts black and
  fades in. Fades are best effort: a property mpv refuses is skipped, and brightness and
  volume are always restored, so a fade problem can't leave the screen dark or stop the
  playlist.
- **mpv runs permanently** with `--idle=yes --force-window=yes` (a black screen when there's
  nothing to play). Artwork is switched with `loadfile`, never by restarting mpv.
  `MpvIpc` opens a short-lived connection per call, so there is no stale connection after
  an mpv restart.
- mpv outputs directly via DRM/KMS: `--vo=gpu --gpu-context=drm`, with `--drm-device` and
  `--drm-connector` taken from `/sys/class/drm`, because card0 vs card1 numbering differs
  between kernels. Audio is ALSA, `alsa/hdmi:CARD=vc4hdmiN,DEV=0` for the connected port.
  The vc4 HDMI driver needs the IEC958 `hdmi:` PCM, not `hw:`.
- **Hardware decoding.** The `hwdec` setting defaults to `"auto"`, which `hwdec_arg()`
  turns into `v4l2m2m-copy,auto-safe` (mpv ≥ 0.38) or `v4l2m2m-copy` (0.35 ignores a
  list). mpv's `auto-safe` alone skips the Pi 4's V4L2 H.264 decoder; measured on the
  Pi, software decoding took 220-320% CPU for a 1080p H.264 file, against ~40% in hardware.
- `--audio-fallback-to-null=yes`: if HDMI audio fails, video keeps playing. mpv never
  retries by itself, so the supervisor checks `current-ao` and sends `ao-reload` to a
  player stuck on `"null"` (after 30 s, backing off to 10 min). This doesn't interrupt
  the picture.
- **Graphics hangs.** If the VideoCore firmware hangs (seen on a 1 GB Pi 4 running out of
  memory while opening a 2880×1620 video), mpv blocks in the kernel and ignores even
  SIGKILL. So the supervisor never waits without a limit for a process. When mpv can't be
  stopped it records the file in `/var/lib/frame/hang.json` and exits with code 75, and
  `ExecStopPost` in the unit reboots the Pi (at most 3 reboots in 6 hours, counted in
  that file). After the reboot the file is skipped until the playlist is next edited.
  The freeze watchdog skips a frozen file the same way, within the running session.
- **Upload warnings** (`mediainfo.py`) read container headers in Python (MP4/MOV,
  Matroska/WebM, GIF) to flag files a Pi 4 can't decode in hardware. No ffprobe, and no
  extra mpv, because RAM is tight. They only advise; nothing is refused.
- **Separate soundtracks** play in the second, audio-only mpv (`/run/frame/audio.sock`,
  `build_audio_args`), so a short GIF can loop over a long track. The vc4 HDMI PCM can
  only be opened once, so only one mpv may have audio: with a soundtrack the artwork mpv
  runs with `aid=no` (mpv then closes its audio output), and the audio mpv idles with no
  file (device closed) otherwise. The Director switches in an order that releases the
  device before the other player takes it. Sounds only play while a visual is showing.
- Transparent images are blended on black, not mpv's default checkerboard. The option
  was renamed in mpv 0.38 (`--alpha=blend` before, `--background=color` after), so the
  supervisor reads `mpv --version` and `alpha_args()` picks the right one.
- SVG uploads are rendered to PNG with `rsvg-convert` (package `librsvg2-bin`), because
  Bookworm's mpv can't open SVG. Animated WebP is refused at upload: no ffmpeg before 8.0
  decodes it.
- Rotation (`video-rotate`) and fit (`keepaspect`/`panscan`) are mpv properties, so they
  change live. Display rotation is not done at the KMS level.
- Hardware-specific code is isolated in `display.py` (sysfs/procfs) and `player.py`
  (mpv args). Both are tested with fakes.
- `web.py:check_access()` is the single hook for future authentication. It currently only
  refuses cross-origin POSTs. Control endpoints require `application/json`, which forces
  a CORS preflight, as a cheap CSRF guard.
- **Saved playlists** live in `state.json` under `saved` (name → the `SAVED_KEYS`
  settings) and are edited independently of what plays. `active` names the one that's
  playing: loading one copies its settings over the live ones and sets `active`, and
  `edit_playlist` writes to the live settings too only when it's the active one. Direct
  changes to what's playing ("Play now", `/api/playlist`, `/api/sounds`, `/api/add`)
  clear `active`, so they never change a saved playlist. The Director ignores all this.
  Settings from before `active` existed fall back to matching contents
  (`Controller.active_name`).
- **The UI** is one server-rendered page with four tab views (`#now`, `#playlists`,
  `#library`, `#settings`) plus one editor view per saved playlist (`#edit/<name>`),
  switched by `app.js`; actions save and reload the page, which
  keeps the tab and scroll position.
- **Thumbnails** (`thumbs.py`): a 480 px JPEG per visual in `/var/lib/frame/thumbs`, made
  by a short-lived `mpv --vo=image` (no ffmpeg on the Pi) in one background thread of the
  web service, after upload and once at startup for files without one. Requests never
  wait: `/thumb/<name>` answers 404 until it exists and the page retries. The mpv runs
  at nice 19 with OOM score 1000 inside frame-web's MemoryMax, and the unit has
  `OOMPolicy=continue`, so a huge file can only kill the thumbnailer. Failures are
  remembered (`<name>.failed`) until the file changes. Phones can't be trusted to show a
  video's first frame themselves (iOS shows nothing until it plays).
- **Jumping**: a "next" request file may contain a filename; the Director then switches
  straight to that item if it's in the list.
- Uploads stream to `/var/lib/frame/media/.incoming/` (same filesystem as the media), are
  checked by extension and magic bytes, then renamed into place. waitress spools request
  bodies to `TMPDIR=/var/lib/frame/tmp`, never to RAM or tmpfs.

## File locations (installed)

| Path | What |
|---|---|
| `/opt/frame/app` | copy of this repo's package (rsync'd by the installer) |
| `/opt/frame/venv` | Python virtualenv (Flask, waitress, frame) |
| `/opt/frame/scripts/doctor.sh` | diagnostics, symlinked to `/usr/local/bin/frame-doctor` |
| `/var/lib/frame/media/` | artwork (never touched by install/upgrade) |
| `/var/lib/frame/state.json` | persistent settings (see `state.DEFAULTS`) |
| `/var/lib/frame/hang.json` | recent hang reboots and the file that was playing (written only by the player) |
| `/var/lib/frame/tmp/` | waitress upload spool |
| `/var/lib/frame/thumbs/` | thumbnails (`<name>.jpg`), made and cleaned up by frame-web |
| `/run/frame/mpv.sock` | artwork mpv IPC socket (`/run/frame` is created via `/etc/tmpfiles.d/frame.conf`) |
| `/run/frame/audio.sock` | soundtrack mpv IPC socket |
| `/run/frame/player.json` | supervisor health info and what's playing, read by the UI |
| `/run/frame/network.json` | Wi-Fi mode (`wifi`, `hotspot`, ...), written by frame-netwatch |
| `/run/frame/next-visual`, `next-sound` | "skip now" requests from the web UI (deleted by the Director) |
| `/etc/default/frame` | env overrides (`FRAME_*`, see `config.py`), incl. the generated hotspot password; mode 0600 |
| `/etc/systemd/system/frame-*.service` | units (source in `systemd/`) |

Logs go to journald only: `journalctl -u frame-player -u frame-web`. The installer makes
the journal persistent (`/etc/systemd/journald.conf.d/90-frame.conf`, 32 MB cap), because
Raspberry Pi OS keeps it in RAM and the cause of a problem before a reboot is otherwise lost.

## Development principles

- Simple, boring and reliable beats clever. One person must be able to maintain it.
- Python + Flask + waitress only. No JS frameworks, no build step, no Docker, no
  database server, no Node.
- Never run the web app as root. Keep the systemd hardening in the units.
- Log events (startup, artwork change, upload, delete, errors, restarts), not periodic
  status. Never log every poll.
- Recovery is automatic and endless but backed off: no tight crash loops, and no giving up.
- Don't put flags in `build_mpv_args` that older mpv (0.35, Bookworm) doesn't know: mpv
  exits on unknown options. Check with the integration test on both Debian versions
  (see below).

## How to test

```bash
pip install -r requirements-dev.txt
pytest            # all tests; real-mpv tests auto-skip without mpv+ffmpeg+AF_UNIX
ruff check .
```

To test against the real Pi OS mpv versions on a dev machine (Docker is only a test tool
here, never part of the deployment):

```bash
docker run --rm -v "$PWD:/src:ro" debian:bookworm-slim bash -c '
  apt-get update -qq && apt-get install -y -qq --no-install-recommends mpv ffmpeg python3 python3-venv shellcheck >/dev/null
  cp -r /src /w && cd /w && python3 -m venv /v && /v/bin/pip install -q -r requirements-dev.txt
  /v/bin/pytest -q && shellcheck --severity=warning scripts/*.sh'
```

(Repeat with `debian:trixie-slim`. Add `librsvg2-bin` to run the SVG tests too. If
Debian's mirrors aren't reachable, `ubuntu:jammy` (mpv 0.34) and `ubuntu:questing`
(mpv 0.40) bracket the Pi OS versions.) CI (`.github/workflows/ci.yml`) runs lint, tests and
shellcheck on Python 3.11 and 3.13.

## Deployment

On the Pi: `git clone` → `sudo ./scripts/install.sh` → `sudo reboot`. Upgrades:
`git pull && sudo ./scripts/install.sh`. The installer is idempotent, never deletes media or
state, and restarts running services. `doctor.sh` must stay read-only, and must never block on
`vcgencmd`: it hangs forever when the graphics firmware has.

## Non-goals (for now)

Cloud hosting, user accounts, internet/remote control, Spotify, a marketplace,
AI generation, automatic transcoding, motion sensors, Home Assistant, complex schedules,
multi-screen, mobile apps, Docker. Scheduled screen on/off is a planned future feature:
keep display power logic in `display.py`, driven by the player service.
