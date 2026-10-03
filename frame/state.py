"""Persistent settings stored as a small JSON file.

The web service is the only writer; the player service only reads. Writes are
atomic (temp file + fsync + rename) so a power cut leaves either the old or the
new file, never a half-written one.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

ROTATIONS = (0, 90, 180, 270)
FIT_MODES = ("fit", "fill", "stretch")
# "smooth" for photos and video; "sharp" keeps pixel art crisp when scaled up.
SCALING_MODES = ("smooth", "sharp")

DEFAULTS: dict[str, Any] = {
    # Filename (not path) inside the media directory, or None for a black screen.
    "current": None,
    # Audio filename played (and looped) instead of the artwork's own sound, or None.
    "soundtrack": None,
    "volume": 70,
    "muted": False,
    "rotation": 0,
    "fit": "fit",
    "scaling": "smooth",
    # "auto" = pick the HDMI port the display is connected to. Otherwise an mpv
    # audio device name such as "alsa/hdmi:CARD=vc4hdmi0,DEV=0".
    "audio_device": "auto",
    # mpv --hwdec value. "auto-safe" works on Pi 4; see README for alternatives.
    "hwdec": "auto-safe",
}


class StateError(ValueError):
    pass


def validate(changes: dict[str, Any]) -> dict[str, Any]:
    """Check and normalise a dict of setting changes. Raises StateError."""
    out: dict[str, Any] = {}
    for key, value in changes.items():
        if key not in DEFAULTS:
            raise StateError(f"unknown setting: {key}")
        if key in ("current", "soundtrack"):
            if value is not None and (not isinstance(value, str) or not value):
                raise StateError(f"{key} must be a filename or null")
        elif key == "volume":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise StateError("volume must be a number 0-100")
            value = int(round(min(100, max(0, value))))
        elif key == "muted":
            if not isinstance(value, bool):
                raise StateError("muted must be true or false")
        elif key == "rotation":
            if isinstance(value, bool) or value not in ROTATIONS:
                raise StateError("rotation must be one of 0, 90, 180, 270")
            value = int(value)
        elif key == "fit":
            if value not in FIT_MODES:
                raise StateError(f"fit must be one of {', '.join(FIT_MODES)}")
        elif key == "scaling":
            if value not in SCALING_MODES:
                raise StateError(f"scaling must be one of {', '.join(SCALING_MODES)}")
        elif key in ("audio_device", "hwdec"):
            if not isinstance(value, str) or not value or "\n" in value:
                raise StateError(f"{key} must be a non-empty string")
        out[key] = value
    return out


class StateStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def load(self) -> dict[str, Any]:
        """Return the saved state merged over defaults. Never raises for bad files."""
        state = dict(DEFAULTS)
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return state
        except (OSError, ValueError) as exc:
            log.warning("state file %s unreadable (%s); using defaults", self.path, exc)
            self._quarantine()
            return state
        if not isinstance(raw, dict):
            log.warning("state file %s is not a JSON object; using defaults", self.path)
            return state
        # Validate key by key so one bad value doesn't discard everything else.
        for key, value in raw.items():
            try:
                state.update(validate({key: value}))
            except StateError as exc:
                log.warning("ignoring invalid saved setting %s: %s", key, exc)
        return state

    def update(self, **changes: Any) -> dict[str, Any]:
        """Validate, merge and persist changes. Returns the new full state."""
        clean = validate(changes)
        with self._lock:
            state = self.load()
            state.update(clean)
            self._write(state)
        return state

    def _write(self, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        data = json.dumps(state, indent=2, sort_keys=True) + "\n"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)
        _fsync_dir(self.path.parent)

    def _quarantine(self) -> None:
        try:
            os.replace(self.path, self.path.with_name(self.path.name + ".corrupt"))
        except OSError:
            pass


def _fsync_dir(path: Path) -> None:
    # Make the rename durable. Not supported on Windows; harmless to skip there.
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
