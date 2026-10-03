"""What the web UI and API can do, independent of HTTP.

Rule of thumb: persist first, then tell mpv. If mpv is down, the saved state is
applied by the player service when it starts mpv again, so the user's choice is
never lost.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from . import display
from .config import Config
from .media import MediaError, MediaLibrary
from .player import InvalidMedia, MpvIpc, PlayerError, PlayerUnavailable, fit_properties
from .state import StateStore

log = logging.getLogger("frame.control")


class Controller:
    def __init__(self, cfg: Config, ipc: MpvIpc | None = None):
        self.cfg = cfg
        self.state = StateStore(cfg.state_file)
        self.library = MediaLibrary(cfg.media_dir, cfg.incoming_dir)
        self.ipc = ipc or MpvIpc(cfg.mpv_socket)

    # --- status -------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        state = self.state.load()
        out: dict[str, Any] = {
            "current": state["current"],
            "volume": state["volume"],
            "muted": state["muted"],
            "rotation": state["rotation"],
            "fit": state["fit"],
            "audio_device": state["audio_device"],
            "hwdec": state["hwdec"],
            "player": {"reachable": False},
            "playback": "unknown",
            "system_uptime": _system_uptime(),
        }
        try:
            paused = self.ipc.get("pause", False)
            idle = self.ipc.get("idle-active", True)
            out["player"] = {
                "reachable": True,
                "hwdec_current": self.ipc.get("hwdec-current"),
                "playing_file": _basename(self.ipc.get("path")),
                "audio_device": self.ipc.get("audio-device"),
            }
            out["playback"] = "idle" if idle else ("paused" if paused else "playing")
        except PlayerUnavailable:
            out["playback"] = "player offline"
        out["player"].update(self._supervisor_status())
        return out

    def _supervisor_status(self) -> dict[str, Any]:
        try:
            data = json.loads(self.cfg.player_status_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        started = data.get("mpv_started_at")
        return {
            "mpv_running": data.get("mpv_running"),
            "mpv_uptime": round(time.time() - started) if started else None,
            "restarts": data.get("restarts"),
            "display": data.get("display"),
        }

    # --- playback -----------------------------------------------------------

    def play(self, name: str) -> dict[str, Any]:
        """Select artwork. Returns {"warning": ...} if the player is offline."""
        path = self.library.resolve(name)
        previous = self.state.load()["current"]
        try:
            self.ipc.load(path)
            self._quiet_set("pause", False)
        except InvalidMedia as exc:
            log.error("invalid media %s: %s", name, exc)
            self._restore(previous)
            raise MediaError(f"cannot play {name}: {exc}", 422) from exc
        except PlayerUnavailable:
            self.state.update(current=name)
            log.warning("artwork set to %s but player is offline; it will start with it", name)
            return {"warning": "Player is not running; the artwork will start when it recovers."}
        self.state.update(current=name)
        log.info("artwork changed to %s", name)
        return {}

    def _restore(self, previous: str | None) -> None:
        try:
            if previous and self.library.exists(previous):
                self.ipc.load(self.library.resolve(previous))
            else:
                self.ipc.command("stop")
        except PlayerError as exc:
            log.warning("could not restore previous artwork: %s", exc)

    def stop(self) -> None:
        """Show a black screen and forget the current artwork."""
        self.state.update(current=None)
        self._quiet_command("stop")
        log.info("playback stopped")

    def pause(self) -> None:
        self.ipc.set("pause", True)
        log.info("paused")

    def resume(self) -> None:
        self.ipc.set("pause", False)
        log.info("resumed")

    def set_volume(self, volume: Any) -> int:
        state = self.state.update(volume=volume)
        self._quiet_set("volume", state["volume"])
        return state["volume"]

    def set_muted(self, muted: bool | None = None) -> bool:
        if muted is None:
            muted = not self.state.load()["muted"]
        state = self.state.update(muted=muted)
        self._quiet_set("mute", state["muted"])
        log.info("muted" if state["muted"] else "unmuted")
        return state["muted"]

    def update_settings(self, changes: dict[str, Any]) -> dict[str, Any]:
        """Display/audio settings. All are applied live, without restarting mpv."""
        allowed = {"rotation", "fit", "audio_device", "hwdec"}
        unknown = set(changes) - allowed
        if unknown:
            raise MediaError(f"unknown setting(s): {', '.join(sorted(unknown))}")
        state = self.state.update(**changes)
        if "rotation" in changes:
            self._quiet_set("video-rotate", state["rotation"])
        if "fit" in changes:
            for prop, value in fit_properties(state["fit"]).items():
                self._quiet_set(prop, value)
        if "hwdec" in changes:
            self._quiet_set("hwdec", state["hwdec"])
        if "audio_device" in changes:
            device = state["audio_device"]
            if device == "auto":
                # Same rule the player service uses at startup.
                connector = display.pick_connector(display.list_connectors(self.cfg.sys_drm))
                device = display.hdmi_audio_device(connector, self.cfg.proc_asound) or "auto"
            self._quiet_set("audio-device", device)
        log.info("settings changed: %s", ", ".join(f"{k}={state[k]}" for k in changes))
        return {k: state[k] for k in allowed}

    def audio_devices(self) -> list[dict[str, str]]:
        try:
            devices = self.ipc.get("audio-device-list", []) or []
        except PlayerUnavailable:
            return []
        return [
            {"name": d.get("name", ""), "description": d.get("description", "")}
            for d in devices
            if d.get("name")
        ]

    # --- library ------------------------------------------------------------

    def media(self) -> list[dict[str, Any]]:
        current = self.state.load()["current"]
        return [dict(item.to_dict(), current=item.name == current) for item in self.library.list()]

    def delete(self, name: str, force: bool = False) -> None:
        self.library.resolve(name)  # validates / 404s first
        if name == self.state.load()["current"]:
            if not force:
                raise MediaError(
                    f"{name} is currently playing; choose other artwork first "
                    "or delete with force to stop playback",
                    409,
                )
            self.stop()
        self.library.delete(name)
        log.info("deleted %s", name)

    # --- helpers ------------------------------------------------------------

    def _quiet_set(self, prop: str, value: Any) -> None:
        """Set an mpv property; if mpv is down the saved state covers it."""
        try:
            self.ipc.set(prop, value)
        except PlayerUnavailable:
            pass
        except PlayerError as exc:
            log.warning("mpv rejected %s=%r: %s", prop, value, exc)

    def _quiet_command(self, *args: Any) -> None:
        try:
            self.ipc.command(*args)
        except PlayerError as exc:
            log.debug("mpv command %s failed: %s", args[0], exc)


def _basename(path: Any) -> str | None:
    if not isinstance(path, str):
        return None
    return path.replace("\\", "/").rsplit("/", 1)[-1]


def _system_uptime() -> float | None:
    try:
        with open("/proc/uptime") as f:
            return float(f.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None
