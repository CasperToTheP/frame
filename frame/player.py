"""mpv control: command-line construction and the JSON IPC client.

Both services use this module. The player service builds the mpv command line;
the web service talks to the already-running mpv through its IPC socket
(https://mpv.io/manual/stable/#json-ipc).
"""

from __future__ import annotations

import itertools
import json
import logging
import re
import socket
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class PlayerError(Exception):
    """mpv rejected a command."""


class PlayerUnavailable(PlayerError):
    """mpv is not running or its IPC socket can't be reached."""


class InvalidMedia(PlayerError):
    """mpv could not play the file (corrupt or unsupported)."""


# --- settings -> mpv ---------------------------------------------------------


def fit_properties(fit: str) -> dict[str, Any]:
    """mpv properties for a fit mode. Black bars are mpv's default background."""
    if fit == "fill":  # crop to fill the screen, keep aspect
        return {"keepaspect": True, "panscan": 1.0}
    if fit == "stretch":  # distort to fill the screen
        return {"keepaspect": False, "panscan": 0.0}
    return {"keepaspect": True, "panscan": 0.0}  # "fit": letterbox / pillarbox


def scale_filter(scaling: str) -> str:
    """mpv upscaler. Bilinear is cheap on the Pi; nearest keeps pixel art blocky."""
    return "nearest" if scaling == "sharp" else "bilinear"


def mpv_version(mpv_bin: str) -> tuple[int, int] | None:
    """(major, minor) of the installed mpv, or None if it can't be determined."""
    try:
        out = subprocess.run(
            [mpv_bin, "--no-config", "--version"],
            capture_output=True, text=True, timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_mpv_version(out)


def parse_mpv_version(text: Any) -> tuple[int, int] | None:
    """(major, minor) from "mpv v0.40.0 ..." or mpv's ``mpv-version`` property."""
    m = re.search(r"mpv v?(\d+)\.(\d+)", text) if isinstance(text, str) else None
    return (int(m.group(1)), int(m.group(2))) if m else None


def hwdec_arg(setting: str | None, version: tuple[int, int] | None) -> str:
    """mpv's --hwdec value for the saved setting.

    "auto" is Frame's default. mpv's own "auto-safe" skips the Pi 4's H.264
    decoder (V4L2), so H.264 was decoded in software: 2-3 of the 4 CPU cores for
    a 1080p video, against about half a core with ``v4l2m2m-copy``. HEVC needs
    the DRM decoder, which "auto-safe" does pick. mpv 0.40 (Trixie) takes a list
    and tries them in order; 0.35 (Bookworm) ignores a list, so it gets the H.264
    decoder only. Files the hardware can't do (above 1080p) fall back to software.
    """
    if setting and setting != "auto":
        return setting
    if version is not None and version >= (0, 38):
        return "v4l2m2m-copy,auto-safe"
    return "v4l2m2m-copy"


def alpha_args(version: tuple[int, int] | None) -> list[str]:
    """Show transparent images on black instead of mpv's default checkerboard.

    The option was renamed in mpv 0.38, and mpv exits on options it doesn't
    know, so the right one is picked by version (and none if it is unknown).
    """
    if version is None:
        return []
    if version >= (0, 38):
        return ["--background=color"]  # blended against --background-color (black)
    return ["--alpha=blend"]  # blended against --background (black)


def _opt(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def build_mpv_args(
    mpv_bin: str,
    socket_path: Path,
    state: dict[str, Any],
    media_path: Path | None,
    drm_device: str | None = None,
    drm_connector: str | None = None,
    audio_device: str | None = None,
    mpv_version: tuple[int, int] | None = None,
    own_audio: bool = True,
) -> list[str]:
    """The full mpv command line for the player service.

    mpv stays running with ``--idle`` even with nothing to play, so the screen is
    black (not a console) and the web UI can always ``loadfile`` new artwork.
    ``own_audio=False`` mutes the artwork's own sound track (``--aid=no``)
    because a separate soundtrack plays in the audio-only mpv instead.
    """
    args = [
        mpv_bin,
        "--no-config",  # ignore any ~/.config/mpv; everything is set here
        # Output straight to the display via DRM/KMS: no X11/Wayland desktop needed.
        "--vo=gpu",
        "--gpu-context=drm",
        f"--hwdec={hwdec_arg(state.get('hwdec'), mpv_version)}",
        "--fullscreen",
        "--idle=yes",
        "--force-window=yes",  # draw a black screen while idle
        # No UI of any kind on the frame.
        "--no-osc",
        "--no-osd-bar",
        "--osd-level=0",
        "--cursor-autohide=always",
        "--no-input-default-bindings",
        "--input-terminal=no",
        "--audio-display=no",  # never show embedded cover art
        "--sub-auto=no",
        # Don't load built-in scripts we never use (saves RAM on a 1 GB Pi).
        "--ytdl=no",
        "--load-stats-overlay=no",
        "--load-auto-profiles=no",
        # Looping. Images stay up forever; video/GIF loops by seeking to the start.
        "--loop-file=inf",
        "--image-display-duration=inf",
        # Keep up to 32 MiB of already-played data so short loops restart from RAM.
        "--cache=yes",
        "--demuxer-max-bytes=32MiB",
        "--demuxer-max-back-bytes=32MiB",
        # Audio over ALSA (Raspberry Pi OS Lite has no sound server by default).
        # If the audio device fails (e.g. monitor off), keep the video playing.
        "--ao=alsa",
        "--audio-fallback-to-null=yes",
        "--volume-max=100",
        f"--volume={int(state.get('volume', 70))}",
        f"--mute={_opt(bool(state.get('muted')))}",
        f"--video-rotate={int(state.get('rotation', 0))}",
        f"--scale={scale_filter(state.get('scaling', 'smooth'))}",
        *alpha_args(mpv_version),
        # Warnings and errors go to the journal; --quiet drops the "AV: ..." status line.
        "--quiet",
        "--msg-level=all=warn",
        f"--input-ipc-server={socket_path}",
    ]
    for key, value in fit_properties(state.get("fit", "fit")).items():
        args.append(f"--{key}={_opt(value)}")
    if drm_device:
        args.append(f"--drm-device={drm_device}")
    if drm_connector:
        args.append(f"--drm-connector={drm_connector}")
    if audio_device:
        args.append(f"--audio-device={audio_device}")
    if not own_audio:
        args.append("--aid=no")
    if media_path is not None:
        args += ["--", str(media_path)]
    return args


def build_audio_args(
    mpv_bin: str,
    socket_path: Path,
    state: dict[str, Any],
    audio_path: Path | None,
    audio_device: str | None = None,
) -> list[str]:
    """Command line for the audio-only mpv that loops a separate soundtrack.

    It idles (with the audio device closed) while there is no soundtrack, so it
    never competes with the main player for the HDMI audio device.
    """
    args = [
        mpv_bin,
        "--no-config",
        "--no-video",
        "--force-window=no",
        "--idle=yes",
        "--no-input-default-bindings",
        "--input-terminal=no",
        "--audio-display=no",
        "--cover-art-auto=no",  # don't pick up images next to the audio file
        "--ytdl=no",
        "--load-stats-overlay=no",
        "--load-auto-profiles=no",
        "--loop-file=inf",
        "--cache=yes",
        "--demuxer-max-bytes=32MiB",
        "--demuxer-max-back-bytes=32MiB",
        "--ao=alsa",
        "--audio-fallback-to-null=yes",
        "--volume-max=100",
        f"--volume={int(state.get('volume', 70))}",
        f"--mute={_opt(bool(state.get('muted')))}",
        "--quiet",
        "--msg-level=all=warn",
        f"--input-ipc-server={socket_path}",
    ]
    if audio_device:
        args.append(f"--audio-device={audio_device}")
    if audio_path is not None:
        args += ["--", str(audio_path)]
    return args


# --- IPC client --------------------------------------------------------------


def _unix_connect(path: str, timeout: float):
    if not hasattr(socket, "AF_UNIX"):
        raise PlayerUnavailable("unix sockets are not supported on this platform")
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(path)
    except OSError:
        s.close()
        raise
    return s


class MpvIpc:
    """Small synchronous mpv JSON-IPC client.

    Each call opens its own short-lived connection. That costs microseconds on a
    local socket and means there is no long-lived connection to go stale when
    mpv restarts, which is what makes this robust.
    """

    _ids = itertools.count(1)

    def __init__(
        self,
        socket_path: Path,
        timeout: float = 2.0,
        connect: Callable[[str, float], Any] = _unix_connect,
    ):
        self.socket_path = str(socket_path)
        self.timeout = timeout
        self._connect = connect

    # Low level ---------------------------------------------------------------

    def _open(self):
        try:
            return _Conn(self._connect(self.socket_path, self.timeout))
        except (OSError, PlayerUnavailable) as exc:
            raise PlayerUnavailable(f"player not reachable ({exc})") from exc

    def command(self, *args: Any) -> Any:
        conn = self._open()
        try:
            return conn.request(list(args), next(self._ids))
        finally:
            conn.close()

    # Convenience -------------------------------------------------------------

    def get(self, name: str, default: Any = None) -> Any:
        try:
            return self.command("get_property", name)
        except PlayerUnavailable:
            raise
        except PlayerError:
            # e.g. "property unavailable" for 'path' while idle
            return default

    def set(self, name: str, value: Any) -> None:
        self.command("set_property", name, value)

    def ping(self) -> bool:
        try:
            self.command("get_property", "pid")
            return True
        except PlayerError:
            return False

    def load(self, path: Path, wait: float = 6.0) -> None:
        """Replace the current file and wait until mpv has opened it.

        Raises InvalidMedia if mpv reports an error for the file. If mpv doesn't
        report either way within ``wait`` seconds we assume it is fine (slow SD
        card); the player keeps working either way.
        """
        conn = self._open()
        try:
            conn.request(["loadfile", str(path), "replace"], next(self._ids))
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline:
                msg = conn.read_message(deadline)
                if msg is None:
                    break
                event = msg.get("event")
                if event == "file-loaded":
                    return
                if event == "end-file" and msg.get("reason") == "error":
                    raise InvalidMedia(msg.get("file_error") or "mpv could not open the file")
            log.warning("no load confirmation from mpv for %s; assuming ok", path)
        finally:
            conn.close()


class _Conn:
    def __init__(self, sock):
        self.sock = sock
        self.buf = b""

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass

    def request(self, command: list, request_id: int) -> Any:
        payload = json.dumps({"command": command, "request_id": request_id}) + "\n"
        try:
            self.sock.sendall(payload.encode("utf-8"))
        except OSError as exc:
            raise PlayerUnavailable(f"player connection lost ({exc})") from exc
        while True:
            msg = self.read_message(None)
            if msg is None:
                raise PlayerUnavailable("player closed the connection")
            if "event" in msg or msg.get("request_id") != request_id:
                continue  # unrelated async event
            if msg.get("error") != "success":
                raise PlayerError(f"{command[0]}: {msg.get('error')}")
            return msg.get("data")

    def read_message(self, deadline: float | None) -> dict | None:
        """Next JSON line, or None on EOF / deadline."""
        while b"\n" not in self.buf:
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self.sock.settimeout(remaining)
            try:
                chunk = self.sock.recv(65536)
            except TimeoutError:
                if deadline is not None:
                    return None
                raise PlayerUnavailable("player did not answer in time") from None
            except OSError as exc:
                raise PlayerUnavailable(f"player connection lost ({exc})") from exc
            if not chunk:
                return None
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        try:
            return json.loads(line)
        except ValueError:
            log.warning("ignoring malformed IPC line from mpv: %r", line[:200])
            return {}
