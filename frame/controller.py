"""What the web UI and API can do, independent of HTTP.

Rule of thumb: persist first, then tell mpv. If mpv is down, the saved state is
applied by the player service when it starts mpv again, so the user's choice is
never lost.

What plays (playlists, soundtracks, black screen) is decided by the player
service's Director: the controller only saves the choice in state.json and the
Director picks it up within half a second. Live settings (volume, pause,
rotation...) still go straight to mpv.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from . import display
from .config import Config
from .director import NEXT_SOUND, NEXT_VISUAL
from .media import VISUAL_KINDS, MediaError, MediaLibrary, kind_of
from .mediainfo import pi_warning
from .netwatch import read_status as read_network_status
from .player import (
    MpvIpc,
    PlayerError,
    PlayerUnavailable,
    fit_properties,
    hwdec_arg,
    parse_mpv_version,
    scale_filter,
)
from .state import SAVED_KEYS, StateStore, saved_name
from .thumbs import Thumbnails

log = logging.getLogger("frame.control")

OFFLINE_WARNING = "Player is not running; your choice is saved and starts when it recovers."


class Controller:
    def __init__(self, cfg: Config, ipc: MpvIpc | None = None,
                 audio_ipc: MpvIpc | None = None):
        self.cfg = cfg
        self.state = StateStore(cfg.state_file)
        self.library = MediaLibrary(cfg.media_dir, cfg.incoming_dir)
        self.ipc = ipc or MpvIpc(cfg.mpv_socket)
        # The audio-only mpv that loops a separate soundtrack.
        self.audio_ipc = audio_ipc or MpvIpc(cfg.audio_socket)
        self.thumbs = Thumbnails(self.library, cfg.thumb_dir, cfg.mpv_bin)

    # --- status -------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        state = self.state.load()
        supervisor = self._supervisor_status()
        now = self._now_playing(state, supervisor.pop("playlist", None))
        out: dict[str, Any] = {
            "current": now["visual"]["current"],
            "soundtrack": now["sound"]["current"],
            "now": now,
            "playlist": state["playlist"],
            "interval": state["interval"],
            "shuffle": state["shuffle"],
            "blank": state["blank"],
            "sounds": state["sounds"],
            "sound_interval": state["sound_interval"],
            "sound_shuffle": state["sound_shuffle"],
            "fade": state["fade"],
            "saved": self._saved_summary(state),
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
            "network": dict(read_network_status(self.cfg.network_status_file),
                            hotspot_ssid=self.cfg.hotspot_ssid,
                            hotspot_password=self.cfg.hotspot_password),
            # Lets the UI turn the player's "next_at" times into countdowns
            # even if the phone's clock is off.
            "server_time": time.time(),
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
        out["player"].update(supervisor)
        return out

    def _now_playing(self, state: dict[str, Any], reported: Any) -> dict[str, Any]:
        """What the player says it plays; failing that, what it will start with."""
        if isinstance(reported, dict) and "visual" in reported and "sound" in reported:
            return reported
        visuals = [] if state["blank"] else self.library.playable(state["playlist"], VISUAL_KINDS)
        sounds = self.library.playable(state["sounds"], ("audio",)) if visuals else []

        def channel(items: list[str]) -> dict[str, Any]:
            return {"current": items[0] if items else None, "position": 1 if items else None,
                    "count": len(items), "next_at": None}

        return {"visual": channel(visuals), "sound": channel(sounds), "paused": False,
                "error": None}

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
            "playlist": data.get("playlist"),
        }

    # --- what plays -----------------------------------------------------------

    def play(self, name: str) -> dict[str, Any]:
        """Show just this artwork (a playlist of one, looping forever)."""
        self._check(name, VISUAL_KINDS, "an audio file; add it to Sound instead")
        self.state.update(playlist=[name], blank=False)
        log.info("artwork set to %s", name)
        return self._offline_warning()

    def add(self, name: str) -> dict[str, Any]:
        """Append a file to the playlist (visuals) or the sound list (audio)."""
        self.library.resolve(name)
        state = self.state.load()
        if kind_of(name) == "audio":
            if name not in state["sounds"]:
                self.state.update(sounds=state["sounds"] + [name])
        else:
            self._check(name, VISUAL_KINDS, "not a visual")
            changes: dict[str, Any] = {"blank": False}
            if name not in state["playlist"]:
                changes["playlist"] = state["playlist"] + [name]
            self.state.update(**changes)
        log.info("added %s", name)
        return self._offline_warning()

    def set_playlist(self, changes: dict[str, Any]) -> dict[str, Any]:
        """Change the visual playlist: items, interval, shuffle."""
        return self._set_list(changes, "playlist", "interval", "shuffle", VISUAL_KINDS)

    def set_sounds(self, changes: dict[str, Any]) -> dict[str, Any]:
        """Change the sound list: items, interval, shuffle. No items = artwork's own sound."""
        return self._set_list(changes, "sounds", "sound_interval", "sound_shuffle", ("audio",))

    def _set_list(self, changes: dict[str, Any], list_key: str, interval_key: str,
                  shuffle_key: str, kinds: tuple[str, ...]) -> dict[str, Any]:
        allowed = {"items", "interval", "shuffle"}
        unknown = set(changes) - allowed
        if unknown:
            raise MediaError(f"unknown field(s): {', '.join(sorted(unknown))}")
        update: dict[str, Any] = {}
        if "items" in changes:
            items = changes["items"]
            if not isinstance(items, list):
                raise MediaError("items must be a list of filenames")
            for name in items:
                if not isinstance(name, str):
                    raise MediaError("items must be a list of filenames")
                self._check(name, kinds, "not allowed in this list")
            update[list_key] = items
            if list_key == "playlist" and items:
                update["blank"] = False
        if "interval" in changes:
            update[interval_key] = changes["interval"]
        if "shuffle" in changes:
            update[shuffle_key] = changes["shuffle"]
        state = self.state.update(**update)
        log.info("%s changed: %s", list_key, ", ".join(f"{k}={state[k]}" for k in update))
        return {"items": state[list_key], "interval": state[interval_key],
                "shuffle": state[shuffle_key], **self._offline_warning()}

    def set_soundtrack(self, name: str | None) -> dict[str, Any]:
        """One sound instead of the artwork's own (None = back to the artwork's own)."""
        result = self.set_sounds({"items": [name] if name else []})
        return {k: v for k, v in result.items() if k == "warning"}

    def next(self, which: str, target: str | None = None) -> dict[str, Any]:
        """Skip to the next visual or sound now (with a fade), or jump to ``target``,
        which must be in that list."""
        request = {"visual": NEXT_VISUAL, "sound": NEXT_SOUND}.get(which)
        if request is None:
            raise MediaError("which must be 'visual' or 'sound'")
        if target is not None:
            items = self.state.load()["playlist" if which == "visual" else "sounds"]
            if not isinstance(target, str) or target not in items:
                raise MediaError("that file isn't in the list")
        self.cfg.run_dir.mkdir(parents=True, exist_ok=True)
        path = self.cfg.run_dir / request
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(target or "", encoding="utf-8")
        os.replace(tmp, path)  # the player never sees a half-written request
        return self._offline_warning()

    def stop(self) -> None:
        """Black screen and silence. The playlists are kept for Start."""
        self.state.update(blank=True)
        log.info("black screen")

    def start(self) -> None:
        self.state.update(blank=False)
        log.info("playback started")

    # --- saved playlists ("Sleeping", "Morning"...) ---------------------------

    def _saved_summary(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        current = {k: state[k] for k in SAVED_KEYS}
        return [
            {"name": name, "count": len(entry["playlist"]), "sound_count": len(entry["sounds"]),
             "first": next(iter(self.library.playable(entry["playlist"], VISUAL_KINDS)), None),
             # Up to four pictures for the cover.
             "covers": self.library.playable(entry["playlist"], VISUAL_KINDS)[:4],
             # The one that's playing now, unless it's been edited since.
             "active": not state["blank"] and entry == current}
            for name, entry in state["saved"].items()
        ]

    def save_playlist(self, name: Any) -> dict[str, Any]:
        """Save the current playlist, sound list and their settings under ``name``.
        An existing playlist with that name is replaced."""
        name = saved_name(name)
        state = self.state.load()
        if not state["playlist"]:
            raise MediaError("the playlist is empty; add some artwork first")
        saved = dict(state["saved"])
        saved[name] = {k: state[k] for k in SAVED_KEYS}
        self.state.update(saved=saved)
        log.info("saved playlist %r", name)
        return {"name": name}

    def load_playlist(self, name: Any) -> dict[str, Any]:
        """Play a saved playlist (its artwork, sounds, timing and fade)."""
        name = saved_name(name)
        entry = self.state.load()["saved"].get(name)
        if entry is None:
            raise MediaError(f"no saved playlist called {name!r}", 404)
        self.state.update(**entry, blank=False)
        log.info("playing saved playlist %r", name)
        return self._offline_warning()

    def delete_playlist(self, name: Any) -> None:
        name = saved_name(name)
        saved = dict(self.state.load()["saved"])
        if saved.pop(name, None) is None:
            raise MediaError(f"no saved playlist called {name!r}", 404)
        self.state.update(saved=saved)
        log.info("deleted saved playlist %r", name)

    def _check(self, name: str, kinds: tuple[str, ...], why: str) -> None:
        self.library.resolve(name)  # 400 for bad names, 404 if missing
        if kind_of(name) not in kinds:
            raise MediaError(f"{name} is {why}")

    def _offline_warning(self) -> dict[str, Any]:
        return {} if self.ipc.ping() else {"warning": OFFLINE_WARNING}

    # --- live playback settings ---------------------------------------------

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
        allowed = {"rotation", "fit", "scaling", "fade", "audio_device", "hwdec"}
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
            try:
                version = parse_mpv_version(self.ipc.get("mpv-version"))
            except PlayerError:
                version = None  # mpv is down: it starts with the saved setting
            self._quiet_set("hwdec", hwdec_arg(state["hwdec"], version))
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
        now = self._now_playing(state, self._supervisor_status().get("playlist"))
        return [
            dict(item.to_dict(),
                 in_playlist=item.name in state["playlist"],
                 in_sounds=item.name in state["sounds"],
                 playing=item.name in (now["visual"]["current"], now["sound"]["current"]),
                 warning=self.heavy_warning(item.name) if item.kind != "audio" else None,
                 thumb=self.thumbs.version(item.name) if item.kind != "audio" else None)
            for item in self.library.list()
        ]

    def heavy_warning(self, name: str) -> str | None:
        """Advice if ``name`` is likely too heavy for a Pi 4 (None if fine or unknown)."""
        return pi_warning(self.library.dir / name)

    def delete(self, name: str, force: bool = False) -> None:
        self.library.resolve(name)  # validates / 404s first
        state = self.state.load()
        lists = {k: state[k] for k in ("playlist", "sounds") if name in state[k]}
        if lists:
            if not force:
                raise MediaError(
                    f"{name} is in the {' and '.join(lists)}; remove it there first "
                    "or delete with force",
                    409,
                )
            self.state.update(**{k: [n for n in v if n != name] for k, v in lists.items()})
        # Saved playlists forget the file too.
        state = self.state.load()
        saved = {
            title: dict(entry, **{k: [n for n in entry[k] if n != name]
                                  for k in ("playlist", "sounds")})
            for title, entry in state["saved"].items()
        }
        if saved != state["saved"]:
            self.state.update(saved=saved)
        self.library.delete(name)
        self.thumbs.remove(name)
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
