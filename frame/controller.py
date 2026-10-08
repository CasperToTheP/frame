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
from .folders import (
    FolderStore,
    clean_path,
    folder_ref,
    is_folder_ref,
    is_within,
    ref_path,
)
from .media import VISUAL_KINDS, MediaError, MediaLibrary, kind_of, safe_name
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
from .state import DEFAULTS, SAVED_KEYS, StateStore, saved_name
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
        self.folders = FolderStore(cfg.library_file)

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
            "folders": self.folder_summary(),
            "active": self.active_name(state),
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
        self.state.update(playlist=[name], blank=False, active=None)
        log.info("artwork set to %s", name)
        return self._offline_warning()

    def add(self, name: str) -> dict[str, Any]:
        """Append a file to the playlist (visuals) or the sound list (audio)."""
        self.library.resolve(name)
        state = self.state.load()
        if kind_of(name) == "audio":
            if name not in state["sounds"]:
                self.state.update(sounds=state["sounds"] + [name], active=None)
        else:
            self._check(name, VISUAL_KINDS, "not a visual")
            changes: dict[str, Any] = {"blank": False, "active": None}
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
        # Changing what's playing directly never edits a saved playlist.
        state = self.state.update(**update, active=None)
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

    @staticmethod
    def active_name(state: dict[str, Any]) -> str | None:
        """The saved playlist that's playing. Settings from before ``active`` existed
        fall back to the one whose contents match what's playing."""
        if state["active"] in state["saved"]:
            return state["active"]
        if state["active"] is None and state["playlist"]:
            current = {k: state[k] for k in SAVED_KEYS}
            return next((n for n, e in state["saved"].items() if e == current), None)
        return None

    def _expanded(self, entry: dict[str, Any]) -> dict[str, Any]:
        """A saved playlist with its folders replaced by the files in them, as played."""
        names = [item.name for item in self.library.list()]
        visuals = [n for n in names if kind_of(n) in VISUAL_KINDS]
        audio = [n for n in names if kind_of(n) == "audio"]
        return dict(entry, playlist=self.folders.expand(entry["playlist"], visuals),
                    sounds=self.folders.expand(entry["sounds"], audio))

    def _refresh_active(self) -> None:
        """Folder contents changed: the playing playlist follows its folders."""
        state = self.state.load()
        name = state["active"]
        entry = state["saved"].get(name) if name else None
        if not entry or not any(is_folder_ref(i) for i in entry["playlist"] + entry["sounds"]):
            return
        played = self._expanded(entry)
        changes = {k: played[k] for k in ("playlist", "sounds") if played[k] != state[k]}
        if changes:
            self.state.update(**changes)
            log.info("playlist %r follows its folders", name)

    def _saved_summary(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        active = self.active_name(state)
        out = []
        for name, entry in state["saved"].items():
            played = self._expanded(entry)
            visuals = self.library.playable(played["playlist"], VISUAL_KINDS)
            out.append({
                "name": name,
                "items": entry["playlist"], "sounds": entry["sounds"],
                "interval": entry["interval"], "shuffle": entry["shuffle"],
                "sound_interval": entry["sound_interval"],
                "sound_shuffle": entry["sound_shuffle"], "fade": entry["fade"],
                "count": len(visuals),
                "sound_count": len(self.library.playable(played["sounds"], ("audio",))),
                "first": visuals[0] if visuals else None,
                "covers": visuals[:4],  # up to four pictures for the cover
                "active": name == active,
                "playing": name == active and not state["blank"],
            })
        return out

    def _saved_entry(self, state: dict[str, Any], name: Any) -> tuple[str, dict[str, Any]]:
        name = saved_name(name)
        entry = state["saved"].get(name)
        if entry is None:
            raise MediaError(f"no playlist called {name!r}", 404)
        return name, entry

    def save_playlist(self, name: Any) -> dict[str, Any]:
        """Save what's playing as a playlist called ``name`` (replacing one with
        that name). It's then the playing playlist."""
        name = saved_name(name)
        state = self.state.load()
        if not state["playlist"]:
            raise MediaError("nothing is playing to save; add some artwork first")
        saved = dict(state["saved"])
        saved[name] = {k: state[k] for k in SAVED_KEYS}
        self.state.update(saved=saved, active=name)
        log.info("saved playlist %r", name)
        return {"name": name}

    def create_playlist(self, name: Any) -> dict[str, Any]:
        """A new, empty playlist. Nothing on the frame changes."""
        name = saved_name(name)
        state = self.state.load()
        if name in state["saved"]:
            raise MediaError(f"there's already a playlist called {name!r}", 409)
        saved = dict(state["saved"])
        saved[name] = {k: DEFAULTS[k] for k in SAVED_KEYS}
        self.state.update(saved=saved)
        log.info("created playlist %r", name)
        return {"name": name}

    def edit_playlist(self, name: Any, changes: dict[str, Any]) -> dict[str, Any]:
        """Change a saved playlist. If it's the one playing, the frame follows.

        ``changes`` may hold: items (artwork), sounds, interval, shuffle,
        sound_interval, sound_shuffle, fade, and rename (a new name).
        """
        allowed = {"items", "sounds", "interval", "shuffle", "sound_interval",
                   "sound_shuffle", "fade", "rename"}
        unknown = set(changes) - allowed
        if unknown:
            raise MediaError(f"unknown field(s): {', '.join(sorted(unknown))}")
        state = self.state.load()
        name, entry = self._saved_entry(state, name)
        entry = dict(entry)
        for key, kinds in (("items", VISUAL_KINDS), ("sounds", ("audio",))):
            if key in changes:
                items = changes[key]
                if not isinstance(items, list) or not all(isinstance(n, str) for n in items):
                    raise MediaError(f"{key} must be a list of filenames")
                folders = self.folders.load()["folders"]
                for item in items:
                    if is_folder_ref(item):
                        if ref_path(item) not in folders:
                            raise MediaError(f"no folder called {ref_path(item)!r}", 404)
                    else:
                        self._check(item, kinds, "not allowed in this list")
                entry["playlist" if key == "items" else "sounds"] = items
        for key in ("interval", "shuffle", "sound_interval", "sound_shuffle", "fade"):
            if key in changes:
                entry[key] = changes[key]
        new_name = saved_name(changes["rename"]) if "rename" in changes else name
        if new_name != name and new_name in state["saved"]:
            raise MediaError(f"there's already a playlist called {new_name!r}", 409)
        # Keep the order of the playlists when renaming.
        saved = {(new_name if n == name else n): (entry if n == name else e)
                 for n, e in state["saved"].items()}
        update: dict[str, Any] = {"saved": saved}
        playing = self.active_name(state) == name
        if playing:
            update.update(self._expanded({k: entry[k] for k in SAVED_KEYS}), active=new_name)
        state = self.state.update(**update)
        log.info("edited playlist %r%s", new_name, " (playing)" if playing else "")
        return {"name": new_name, "playing": playing}

    def add_to_playlist(self, name: Any, filename: str | None = None,
                        folder: str | None = None) -> dict[str, Any]:
        """Append a file to a saved playlist: artwork to its artwork, music to its sound.
        Or a whole folder, which stays linked: what's added to it later plays too."""
        if folder is not None:
            return self._add_folder_to_playlist(name, folder)
        if not isinstance(filename, str):
            raise MediaError("filename or folder is required")
        self.library.resolve(filename)
        _, entry = self._saved_entry(self.state.load(), name)
        if kind_of(filename) == "audio":
            if filename in entry["sounds"]:
                return {"name": saved_name(name), "added": False}
            return dict(self.edit_playlist(name, {"sounds": entry["sounds"] + [filename]}),
                        added=True)
        self._check(filename, VISUAL_KINDS, "not artwork or music")
        if filename in entry["playlist"]:
            return {"name": saved_name(name), "added": False}
        return dict(self.edit_playlist(name, {"items": entry["playlist"] + [filename]}),
                    added=True)

    def _add_folder_to_playlist(self, name: Any, folder: Any) -> dict[str, Any]:
        path = clean_path(folder)
        if path not in self.folders.load()["folders"]:
            raise MediaError(f"no folder called {path!r}", 404)
        _, entry = self._saved_entry(self.state.load(), name)
        ref = folder_ref(path)
        inside = self.folders.expand([ref], [i.name for i in self.library.list()])
        changes: dict[str, Any] = {}
        has_audio = any(kind_of(n) == "audio" for n in inside)
        has_visual = any(kind_of(n) in VISUAL_KINDS for n in inside)
        if (has_visual or not has_audio) and ref not in entry["playlist"]:
            changes["items"] = entry["playlist"] + [ref]
        if has_audio and ref not in entry["sounds"]:
            changes["sounds"] = entry["sounds"] + [ref]
        if not changes:
            return {"name": saved_name(name), "added": False}
        return dict(self.edit_playlist(name, changes), added=True)

    def load_playlist(self, name: Any) -> dict[str, Any]:
        """Play a saved playlist (its artwork, sounds, timing and fade)."""
        name, entry = self._saved_entry(self.state.load(), name)
        played = self._expanded(entry)
        if not self.library.playable(played["playlist"], VISUAL_KINDS):
            raise MediaError(f"“{name}” has no artwork yet; add some first")
        self.state.update(**played, blank=False, active=name)
        log.info("playing playlist %r", name)
        return self._offline_warning()

    def delete_playlist(self, name: Any) -> None:
        """Delete a saved playlist. If it's playing, the frame keeps playing it."""
        state = self.state.load()
        name, _ = self._saved_entry(state, name)
        saved = {n: e for n, e in state["saved"].items() if n != name}
        self.state.update(saved=saved, active=None if state["active"] == name else state["active"])
        log.info("deleted playlist %r", name)

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

    # --- folders --------------------------------------------------------------

    def folder_summary(self) -> list[dict[str, Any]]:
        data = self.folders.load()
        items = self.library.list()
        out = []
        for path in data["folders"]:
            inside = [i for i in items if is_within(data["files"].get(i.name, ""), path)]
            visuals = [i.name for i in inside if i.kind in VISUAL_KINDS]
            out.append({
                "path": path, "name": path.rsplit("/", 1)[-1],
                "parent": path.rsplit("/", 1)[0] if "/" in path else "",
                "count": len(inside), "visuals": len(visuals),
                "audio": len(inside) - len(visuals), "covers": visuals[:4],
            })
        return out

    def create_folder(self, parent: Any, name: Any) -> dict[str, Any]:
        return {"path": self.folders.create(parent, name)}

    def rename_folder(self, path: Any, name: Any) -> dict[str, Any]:
        return self._folder_moved(*self.folders.rename(path, name))

    def move_folder(self, path: Any, parent: Any) -> dict[str, Any]:
        return self._folder_moved(*self.folders.move_folder(path, parent))

    def _folder_moved(self, old: str, new: str) -> dict[str, Any]:
        """Playlists that hold the folder (or one inside it) follow it."""
        self._rewrite_folder_refs(lambda p: new + p[len(old):]
                                  if p == old or p.startswith(old + "/") else p)
        self._refresh_active()
        return {"path": new}

    def delete_folder(self, path: Any) -> dict[str, Any]:
        """The folder goes; its files move up a level. Playlists that held the folder
        keep the files it had, as separate items."""
        path = clean_path(path)
        names = [i.name for i in self.library.list()]
        snapshot = {}
        for p in self.folders.load()["folders"]:
            if is_within(p, path):
                snapshot[folder_ref(p)] = self.folders.expand([folder_ref(p)], names)
        parent = self.folders.delete(path)
        state = self.state.load()
        saved = {}
        for title, entry in state["saved"].items():
            entry = dict(entry)
            for key, kinds in (("playlist", VISUAL_KINDS), ("sounds", ("audio",))):
                items: list[str] = []
                for item in entry[key]:
                    if item in snapshot:
                        items.extend(n for n in snapshot[item] if kind_of(n) in kinds)
                    else:
                        items.append(item)
                entry[key] = list(dict.fromkeys(items))
            saved[title] = entry
        if saved != state["saved"]:
            self.state.update(saved=saved)
        self._refresh_active()
        return {"parent": parent}

    def move_files(self, names: Any, folder: Any) -> dict[str, Any]:
        if not isinstance(names, list) or not names or not all(isinstance(n, str) for n in names):
            raise MediaError("files must be a list of filenames")
        for name in names:
            self.library.resolve(name)
        folder = self.folders.move(names, folder)
        log.info("moved %d file(s) to %s", len(names), folder or "the top of the library")
        self._refresh_active()
        return {"folder": folder, "moved": len(names)}

    def _rewrite_folder_refs(self, change) -> None:
        state = self.state.load()
        saved = {
            title: dict(entry, **{k: [folder_ref(change(ref_path(i))) if is_folder_ref(i) else i
                                      for i in entry[k]] for k in ("playlist", "sounds")})
            for title, entry in state["saved"].items()
        }
        if saved != state["saved"]:
            self.state.update(saved=saved)

    # --- library ------------------------------------------------------------

    def media(self) -> list[dict[str, Any]]:
        state = self.state.load()
        now = self._now_playing(state, self._supervisor_status().get("playlist"))
        files = self.folders.load()["files"]
        return [
            dict(item.to_dict(),
                 folder=files.get(item.name, ""),
                 in_playlist=item.name in state["playlist"],
                 in_sounds=item.name in state["sounds"],
                 playing=item.name in (now["visual"]["current"], now["sound"]["current"]),
                 warning=self.heavy_warning(item.name) if item.kind != "audio" else None,
                 thumb=self.thumbs.version(item.name) if item.kind != "audio" else None)
            for item in self.library.list()
        ]

    def check_uploads(self, files: Any) -> list[dict[str, Any]]:
        """Before uploading: which of the chosen files (``[{"name", "size"}]``) are
        already on the frame, would get a new name, are chosen twice, or can't be
        uploaded at all. Only advice: the page lets the user decide.

        A file is "already there" when a file with the name it would be saved under
        has the same size, or (for anything over 64 KB) any file of the same kind has
        exactly its size: that's a copy saved under another name, or "name-1".
        """
        if not isinstance(files, list) or len(files) > 1000:
            raise MediaError("files must be a list of {name, size}")
        existing = {item.name: item for item in self.library.list()}
        by_size: dict[tuple[str | None, int], list[str]] = {}
        for item in existing.values():
            by_size.setdefault((item.kind, item.size), []).append(item.name)
        folders = self.folders.load()["files"]
        seen: set[tuple[str, int]] = set()
        out = []
        for f in files:
            raw = f.get("name") if isinstance(f, dict) else None
            size = f.get("size") if isinstance(f, dict) else None
            size = size if isinstance(size, int) and size >= 0 else -1
            result: dict[str, Any] = {"name": raw, "status": "ok"}
            out.append(result)
            try:
                stored = safe_name(raw if isinstance(raw, str) else "")
            except MediaError as exc:
                result.update(status="unsupported", message=str(exc))
                continue
            stem, ext = os.path.splitext(stored)
            # What it would be saved as (SVG and animated WebP are converted, so their
            # size on the frame differs; a .gifv is only renamed).
            names = {".svg": [stem + ".png"], ".webp": [stored, stem + ".mp4"],
                     ".gifv": [stem + ".mp4", stem + ".webm"]}.get(ext, [stored])
            same_size = ext not in (".svg", ".webp")
            kind = kind_of(names[0])
            match = next((n for n in names if n in existing and same_size
                          and existing[n].size == size), None)
            if match is None and same_size and size >= 65536:
                match = next(iter(sorted(by_size.get((kind, size), []))), None)
            if (stored.lower(), size) in seen:
                result.update(status="twice", message="chosen twice")
            elif match:
                result.update(status="duplicate", existing=match,
                              folder=folders.get(match, ""), message="already on the frame")
            elif any(n in existing for n in names):
                taken = next(n for n in names if n in existing)
                result.update(status="same_name", existing=taken,
                              folder=folders.get(taken, ""),
                              message=(f"a different file called {taken} is on the frame; "
                                       "this one gets a new name") if same_size else
                                      f"a file called {taken} is already on the frame; "
                                      "uploading adds a copy")
            seen.add((stored.lower(), size))
        return out

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
        self.folders.forget(name)
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
