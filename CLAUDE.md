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

Two systemd services, both running as the unprivileged system user `frame`:

| Service | Module | Role |
|---|---|---|
| `frame-player.service` | `frame/player_service.py` | Supervisor. Starts one mpv with the saved state, restarts it with backoff if it exits, hangs (IPC health check) or has no video output (`vo-configured` false), waits for an HDMI display, and restarts mpv on HDMI hotplug. Writes `/run/frame/player.json`. Never imports Flask. |
| `frame-web.service` | `frame/web.py` (+ `controller.py`) | Flask app served by waitress on :8080. Server-rendered HTML + vanilla JS. Changes playback live over mpv JSON IPC. |

Key rules:

- **Persist first, then tell mpv.** The controller writes `state.json` and then sends the
  IPC command. If mpv is down, the player service applies the saved state when it
  restarts mpv. Only the web service writes `state.json`; the player only reads it.
- **mpv runs permanently** with `--idle=yes --force-window=yes` (a black screen when there's
  nothing to play). Artwork is switched with `loadfile`, never by restarting mpv.
  `MpvIpc` opens a short-lived connection per call, so there is no stale connection after
  an mpv restart.
- mpv outputs directly via DRM/KMS: `--vo=gpu --gpu-context=drm`, with `--drm-device` and
  `--drm-connector` taken from `/sys/class/drm`, because card0 vs card1 numbering differs
  between kernels. Audio is ALSA, `alsa/hdmi:CARD=vc4hdmiN,DEV=0` for the connected port.
  The vc4 HDMI driver needs the IEC958 `hdmi:` PCM, not `hw:`.
- `--audio-fallback-to-null=yes`: if HDMI audio fails, video keeps playing.
- Rotation (`video-rotate`) and fit (`keepaspect`/`panscan`) are mpv properties, so they
  change live. Display rotation is not done at the KMS level.
- Hardware-specific code is isolated in `display.py` (sysfs/procfs) and `player.py`
  (mpv args). Both are tested with fakes.
- `web.py:check_access()` is the single hook for future authentication. It currently only
  refuses cross-origin POSTs. Control endpoints require `application/json`, which forces
  a CORS preflight, as a cheap CSRF guard.
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
| `/var/lib/frame/tmp/` | waitress upload spool |
| `/run/frame/mpv.sock` | mpv IPC socket (created via `/etc/tmpfiles.d/frame.conf`) |
| `/run/frame/player.json` | supervisor health info, read by the UI |
| `/etc/default/frame` | optional env overrides (`FRAME_*`, see `config.py`) |
| `/etc/systemd/system/frame-*.service` | units (source in `systemd/`) |

Logs go to journald only: `journalctl -u frame-player -u frame-web`.

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

(Repeat with `debian:trixie-slim`.) CI (`.github/workflows/ci.yml`) runs lint, tests and
shellcheck on Python 3.11 and 3.13.

## Deployment

On the Pi: `git clone` → `sudo ./scripts/install.sh` → `sudo reboot`. Upgrades:
`git pull && sudo ./scripts/install.sh`. The installer is idempotent, never deletes media or
state, and restarts running services. `doctor.sh` must stay read-only.

## Non-goals (for now)

Cloud hosting, user accounts, internet/remote control, playlists, Spotify, a marketplace,
AI generation, automatic transcoding, motion sensors, Home Assistant, complex schedules,
multi-screen, mobile apps, Docker. Scheduled screen on/off is a planned future feature:
keep display power logic in `display.py`, driven by the player service.
