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
from .media import VISUAL_KINDS, MediaError, MediaLibrary, kind_of
from .player import (
    InvalidMedia,
    MpvIpc,
    PlayerError,
    PlayerUnavailable,
    fit_properties,
    scale_filter,
)
from .state import StateStore

log = logging.getLogger("frame.control")


class Controller:
    def __init__(self, cfg: Config, ipc: MpvIpc | None = None,
                 audio_ipc: MpvIpc | None = None):
        self.cfg = cfg
        self.state = StateStore(cfg.state_file)
        self.library = MediaLibrary(cfg.media_dir, cfg.incoming_dir)
        self.ipc = ipc or MpvIpc(cfg.mpv_socket)
        # The audio-only mpv that loops a separate soundtrack.
        self.audio_ipc = audio_ipc or MpvIpc(cfg.audio_socket)

    # --- status -------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        state = self.state.load()
        out: dict[str, Any] = {
            "current": state["current"],
            "soundtrack": state["soundtrack"],
            "volume": state["volume"],
            "muted": state["muted"],
            "rotation": state["rotation"],
            "fit": state["fit"],
            "scaling": state["scaling"],
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
        try:
            out["player"]["soundtrack_file"] = _basename(self.audio_ipc.get("path"))
        except PlayerUnavailable:
            out["player"]["soundtrack_file"] = None
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
        if kind_of(name) not in VISUAL_KINDS:
            raise MediaError(f"{name} is an audio file; choose it under Sound instead")
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
        self._quiet_sync_sound()
        return {}

    def set_soundtrack(self, name: str | None) -> dict[str, Any]:
        """Play ``name`` (an audio file) instead of the artwork's own sound.

        ``None`` goes back to the artwork's own sound. The soundtrack loops on
        its own, independently of the visual.
        """
        if name is not None:
            self.library.resolve(name)
            if kind_of(name) != "audio":
                raise MediaError(f"{name} is not an audio file")
        previous = self.state.load()["soundtrack"]
        self.state.update(soundtrack=name)
        try:
            self._sync_sound()
        except InvalidMedia as exc:
            log.error("invalid soundtrack %s: %s", name, exc)
            self.state.update(soundtrack=previous)
            self._quiet_sync_sound()
            raise MediaError(f"cannot play {name}: {exc}", 422) from exc
        except PlayerUnavailable:
            log.warning("sound set to %s but player is offline", name or "artwork's own")
            return {"warning": "Player is not running; the sound will start when it recovers."}
        log.info("sound changed to %s", name or "artwork's own")
        return {}

    def _sync_sound(self) -> None:
        """Make the two players match the saved soundtrack choice.

        Only one mpv may hold the HDMI audio device at a time, so the order
        matters: the one that goes quiet releases the device first.
        """
        state = self.state.load()
        path = self.library.soundtrack(state)
        if path is not None:
            self._quiet_set("aid", "no")
            if self.audio_ipc.get("path") != str(path):
                self.audio_ipc.load(path)
            self.audio_ipc.set("pause", False)
        else:
            self._quiet_audio("stop")
            self._wait_audio_idle()
            self._quiet_set("aid", "auto")

    def _quiet_sync_sound(self) -> None:
        try:
            self._sync_sound()
        except PlayerError as exc:
            log.warning("could not restore sound: %s", exc)

    def _wait_audio_idle(self, timeout: float = 1.0) -> None:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                if self.audio_ipc.get("idle-active", True):
                    return
            except PlayerUnavailable:
                return
            time.sleep(0.05)

    def _restore(self, previous: str | None) -> None:
        try:
            if previous and self.library.exists(previous):
                self.ipc.load(self.library.resolve(previous))
            else:
                self.ipc.command("stop")
        except PlayerError as exc:
            log.warning("could not restore previous artwork: %s", exc)

    def stop(self) -> None:
        """Show a black screen and forget the current artwork. Also silences the soundtrack."""
        self.state.update(current=None)
        self._quiet_command("stop")
        self._quiet_audio("stop")
        log.info("playback stopped")

    def pause(self) -> None:
        self.ipc.set("pause", True)
        self._quiet_audio("set", "pause", True)
        log.info("paused")

    def resume(self) -> None:
        self.ipc.set("pause", False)
        self._quiet_audio("set", "pause", False)
        log.info("resumed")

    def set_volume(self, volume: Any) -> int:
        state = self.state.update(volume=volume)
        self._quiet_set("volume", state["volume"])
        self._quiet_audio("set", "volume", state["volume"])
        return state["volume"]

    def set_muted(self, muted: bool | None = None) -> bool:
        if muted is None:
            muted = not self.state.load()["muted"]
        state = self.state.update(muted=muted)
        self._quiet_set("mute", state["muted"])
        self._quiet_audio("set", "mute", state["muted"])
        log.info("muted" if state["muted"] else "unmuted")
        return state["muted"]

    def update_settings(self, changes: dict[str, Any]) -> dict[str, Any]:
        """Display/audio settings. All are applied live, without restarting mpv."""
        allowed = {"rotation", "fit", "scaling", "audio_device", "hwdec"}
        unknown = set(changes) - allowed
        if unknown:
            raise MediaError(f"unknown setting(s): {', '.join(sorted(unknown))}")
        state = self.state.update(**changes)
        if "rotation" in changes:
            self._quiet_set("video-rotate", state["rotation"])
        if "fit" in changes:
            for prop, value in fit_properties(state["fit"]).items():
                self._quiet_set(prop, value)
        if "scaling" in changes:
            self._quiet_set("scale", scale_filter(state["scaling"]))
        if "hwdec" in changes:
            self._quiet_set("hwdec", state["hwdec"])
        if "audio_device" in changes:
            device = state["audio_device"]
            if device == "auto":
                # Same rule the player service uses at startup.
                connector = display.pick_connector(display.list_connectors(self.cfg.sys_drm))
                device = display.hdmi_audio_device(connector, self.cfg.proc_asound) or "auto"
            self._quiet_set("audio-device", device)
            self._quiet_audio("set", "audio-device", device)
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
        state = self.state.load()
        return [
            dict(item.to_dict(), current=item.name == state["current"],
                 soundtrack=item.name == state["soundtrack"])
            for item in self.library.list()
        ]

    def delete(self, name: str, force: bool = False) -> None:
        self.library.resolve(name)  # validates / 404s first
        state = self.state.load()
        if name == state["soundtrack"]:
            if not force:
                raise MediaError(
                    f"{name} is the selected sound; choose another sound first "
                    "or delete with force",
                    409,
                )
            self.set_soundtrack(None)
        if name == state["current"]:
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

    def _quiet_audio(self, *args: Any) -> None:
        """Command or ("set", prop, value) for the audio-only mpv; failures are logged."""
        try:
            if args[0] == "set":
                self.audio_ipc.set(args[1], args[2])
            else:
                self.audio_ipc.command(*args)
        except PlayerUnavailable:
            pass
        except PlayerError as exc:
            log.warning("audio player rejected %s: %s", args, exc)

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
