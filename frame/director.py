"""Playlists: what plays when, and the fades in between.

Runs inside the player service, so playlists keep going even when the web UI is
down. The web service only edits state.json, or asks for the next item by
creating a file in /run/frame; the director notices within one tick.

Two independent channels:
  * visuals, in the artwork mpv. A fade goes through black (mpv's
    ``brightness``) and, if the artwork's own sound is playing, its volume.
  * sounds, in the audio-only mpv. A fade is a volume ramp.

Only one mpv may hold the HDMI audio device at a time (see CLAUDE.md), so when
sounds start or stop, the player that goes quiet releases the device first.
"""

from __future__ import annotations

import logging
import os
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Config
from .media import VISUAL_KINDS, MediaLibrary, kind_of
from .player import InvalidMedia, MpvIpc, PlayerError, PlayerUnavailable
from .state import StateStore

log = logging.getLogger("frame.playlist")

# How long an image stays when the interval is "full length".
IMAGE_SECONDS = 60
# Never switch faster than this, whatever the settings or file lengths.
MIN_SECONDS = 2.0
FADE_STEPS_PER_SECOND = 25
BLACK = -100  # mpv brightness at which the picture is fully black

NEXT_VISUAL = "next-visual"
NEXT_SOUND = "next-sound"


@dataclass
class Channel:
    name: str
    items: list[str] = field(default_factory=list)
    current: str | None = None
    # Seconds the current item has been playing (not counting pauses), as of `since`.
    elapsed: float = 0.0
    since: float = 0.0
    # Seconds the current item stays; None = not known yet (waiting for mpv).
    length: float | None = None
    failed: set[str] = field(default_factory=set)
    # Set while mpv is opening an item, so a hang during loading names the right file.
    loading: str | None = None

    def playable(self) -> list[str]:
        return [n for n in self.items if n not in self.failed]

    def restart_clock(self, now: float) -> None:
        self.elapsed = 0.0
        self.since = now
        self.length = None

    def update_clock(self, now: float, running: bool) -> None:
        if running:
            self.elapsed += max(0.0, now - self.since)
        self.since = now


class Director:
    def __init__(
        self,
        cfg: Config,
        ipc: MpvIpc,
        audio_ipc: MpvIpc,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        rng: random.Random | None = None,
    ):
        self.cfg = cfg
        self.store = StateStore(cfg.state_file)
        self.library = MediaLibrary(cfg.media_dir, cfg.incoming_dir)
        self.ipc = ipc
        self.audio_ipc = audio_ipc
        self.sleep = sleep
        self.clock = clock
        self.rng = rng or random.Random()
        self.state = self.store.load()
        self._mtime = self._state_mtime()
        self.visual = Channel("visual")
        self.sound = Channel("sound")
        self.paused = False
        self.error: str | None = None
        self._last_logged: str | None = None
        # Set whenever something the status file shows has changed.
        self.changed = True

    # --- what should play -----------------------------------------------------

    def _wanted(self) -> tuple[list[str], list[str]]:
        st = self.state
        visuals = [] if st["blank"] else self.library.playable(st["playlist"], VISUAL_KINDS)
        # A black screen is silent: sounds only play alongside a visual.
        sounds = self.library.playable(st["sounds"], ("audio",)) if visuals else []
        return visuals, sounds

    def initial(self) -> tuple[Path | None, Path | None]:
        """The visual and sound mpv should start with after a (re)start."""
        self._reload_state(force=True)
        visuals, sounds = self._wanted()
        self.visual.items, self.sound.items = visuals, sounds
        for ch, shuffle in ((self.visual, self.state["shuffle"]),
                            (self.sound, self.state["sound_shuffle"])):
            if ch.current not in ch.playable():
                ch.current = self._first(ch, shuffle)
            ch.loading = None
            ch.restart_clock(self.clock())
        self._take_request(NEXT_VISUAL)
        self._take_request(NEXT_SOUND)
        self.changed = True
        return self._path(self.visual.current), self._path(self.sound.current)

    def _path(self, name: str | None) -> Path | None:
        return self.library.dir / name if name else None

    def _first(self, ch: Channel, shuffle: bool) -> str | None:
        items = ch.playable()
        if not items:
            return None
        return self.rng.choice(items) if shuffle else items[0]

    def _next(self, ch: Channel, shuffle: bool) -> str | None:
        items = ch.playable()
        if not items:
            return None
        if ch.current not in items:
            # The current item failed or was removed: take the next one after it.
            if ch.current in ch.items:
                start = ch.items.index(ch.current)
                for name in ch.items[start + 1:] + ch.items[:start]:
                    if name in items:
                        return name
            return items[0]
        if shuffle and len(items) > 2:
            return self.rng.choice([n for n in items if n != ch.current])
        return items[(items.index(ch.current) + 1) % len(items)]

    # --- the loop -----------------------------------------------------------

    def tick(self) -> None:
        """Called by the supervisor a few times a second while mpv runs."""
        try:
            if self._reload_state():
                self._reconcile()
            if self._take_request(NEXT_VISUAL):
                self._advance(self.visual)
            if self._take_request(NEXT_SOUND):
                self._advance(self.sound)
            paused = bool(self.ipc.get("pause", False))
            if paused != self.paused:
                self.paused = paused
                self.changed = True
            # Each channel keeps its own clock, so a fade on one doesn't delay the other.
            self._run(self.visual, paused, self.state["interval"], self.ipc)
            self._run(self.sound, paused, self.state["sound_interval"], self.audio_ipc)
        except PlayerUnavailable:
            pass  # mpv is (re)starting; the supervisor's health check handles the rest
        except PlayerError as exc:
            self._note_error(f"player error: {exc}")

    def _run(self, ch: Channel, paused: bool, interval: int, ipc: MpvIpc) -> None:
        ch.update_clock(self.clock(), running=not paused)
        if paused or ch.current is None or len(ch.playable()) < 2:
            return
        if ch.length is None:
            ch.length = self._length(ch, interval, ipc)
            if ch.length is None:
                return
            self.changed = True
        if ch.elapsed >= max(MIN_SECONDS, ch.length - self.state["fade"]):
            self._advance(ch)

    def _length(self, ch: Channel, interval: int, ipc: MpvIpc) -> float | None:
        if interval > 0:
            return float(interval)
        if kind_of(ch.current or "") == "image":
            return float(IMAGE_SECONDS)
        duration = ipc.get("duration")
        if isinstance(duration, (int, float)) and duration > 0:
            return float(duration)
        return None  # not known yet; ask again next tick

    def _advance(self, ch: Channel) -> None:
        shuffle = self.state["shuffle" if ch is self.visual else "sound_shuffle"]
        name = self._next(ch, shuffle)
        if name and name != ch.current:
            self._switch(ch, name)
        else:
            ch.restart_clock(self.clock())

    # --- reacting to changes from the web UI ---------------------------------

    def _state_mtime(self) -> int | None:
        try:
            return os.stat(self.cfg.state_file).st_mtime_ns
        except OSError:
            return None

    def _reload_state(self, force: bool = False) -> bool:
        mtime = self._state_mtime()
        if not force and mtime == self._mtime:
            return False
        self._mtime = mtime
        old = self.state
        self.state = self.store.load()
        timing = ("interval", "sound_interval", "fade")
        if any(old.get(k) != self.state.get(k) for k in timing):
            self.visual.length = self.sound.length = None
            self.changed = True
        return True

    def _reconcile(self) -> None:
        visuals, sounds = self._wanted()
        if visuals != self.visual.items or sounds != self.sound.items:
            self.changed = True
        self.visual.items, self.sound.items = visuals, sounds
        # Files changed: give previously failing files another chance.
        self.visual.failed.clear()
        self.sound.failed.clear()

        def target(ch: Channel, shuffle: bool) -> str | None:
            return ch.current if ch.current in ch.items else self._first(ch, shuffle)

        new_visual = target(self.visual, self.state["shuffle"])
        new_sound = target(self.sound, self.state["sound_shuffle"])
        # Whoever stops using the HDMI audio device goes first.
        steps = [(self.visual, new_visual), (self.sound, new_sound)]
        if new_sound and not self.sound.current:
            steps.reverse()
        for ch, name in steps:
            if name != ch.current:
                self._switch(ch, name, user_choice=True)

    def _take_request(self, name: str) -> bool:
        try:
            os.unlink(self.cfg.run_dir / name)
            return True
        except FileNotFoundError:
            return False
        except OSError as exc:
            log.warning("could not read request %s: %s", name, exc)
            return False

    # --- switching and fading -------------------------------------------------

    def _switch(self, ch: Channel, name: str | None, user_choice: bool = False) -> None:
        for _ in range(max(1, len(ch.items))):
            ch.loading = name
            try:
                if ch is self.visual:
                    self._switch_visual(name, user_choice)
                else:
                    self._switch_sound(name)
            except InvalidMedia as exc:
                ch.loading = None
                self._note_error(f"cannot play {name}: {exc}")
                ch.failed.add(name)
                ch.current = name
                name = self._next(ch, False)
                if name is None:
                    self._switch(ch, None)
                    return
                continue
            ch.loading = None
            ch.current = name
            ch.restart_clock(self.clock())
            self.changed = True
            if name:
                log.info("%s: now %s", "artwork" if ch is self.visual else "sound", name)
            return

    def _volume(self) -> int:
        # Re-read: the user may have moved the volume slider during a fade.
        return int(self.store.load()["volume"])

    def _ramp(self, changes: list[tuple[MpvIpc, str, float, float]], seconds: float) -> None:
        """Move properties smoothly from start to end. Best effort: a property mpv
        refuses (e.g. brightness without a GPU video output) is simply skipped,
        so a fade problem never stops the playlist."""
        steps = max(1, int(seconds * FADE_STEPS_PER_SECOND))
        changes = list(changes)
        for i in range(1, steps + 1):
            for change in list(changes):
                ipc, prop, start, end = change
                try:
                    ipc.set(prop, round(start + (end - start) * i / steps, 2))
                except PlayerUnavailable:
                    raise
                except PlayerError as exc:
                    log.debug("cannot fade %s: %s", prop, exc)
                    changes.remove(change)
            self.sleep(seconds / steps)

    def _switch_visual(self, name: str | None, user_choice: bool) -> None:
        fade = self.state["fade"]
        own_audio = self.sound.current is None
        vol = self._volume()
        showing = self.visual.current is not None
        if fade and showing:
            out = [(self.ipc, "brightness", 0, BLACK)]
            if own_audio:
                out.append((self.ipc, "volume", vol, 0))
            self._ramp(out, fade)
        try:
            if name is None:
                self.ipc.command("stop")
                return
            if fade:
                self._quiet(self.ipc, "brightness", BLACK)
                if own_audio:
                    self._quiet(self.ipc, "volume", 0)
            self.ipc.load(self.library.dir / name)
            if user_choice:
                self.ipc.set("pause", False)
                self._quiet(self.audio_ipc, "pause", False)
            if fade:
                ins = [(self.ipc, "brightness", BLACK, 0)]
                if own_audio:
                    ins.append((self.ipc, "volume", 0, vol))
                self._ramp(ins, fade)
        finally:
            # Whatever happened, never leave the screen dark or the sound down.
            self._quiet(self.ipc, "brightness", 0)
            self._quiet(self.ipc, "volume", self._volume())

    def _switch_sound(self, name: str | None) -> None:
        fade = self.state["fade"]
        vol = self._volume()
        previous = self.sound.current
        if name is None:
            if previous:
                if fade:
                    self._ramp([(self.audio_ipc, "volume", vol, 0)], fade)
                self._quiet_command(self.audio_ipc, "stop")
                self._wait(lambda: self.audio_ipc.get("idle-active", True))
                self._quiet(self.audio_ipc, "volume", self._volume())
            # Give the audio device back to the artwork.
            self._quiet(self.ipc, "aid", "auto")
            return
        if previous is None:
            # Take over from the artwork's own sound: it must release the device first.
            if fade and self.visual.current:
                self._ramp([(self.ipc, "volume", vol, 0)], fade)
            self.ipc.set("aid", "no")
            self._wait(lambda: self.ipc.get("current-ao") is None)
            self.ipc.set("volume", self._volume())
        elif fade:
            self._ramp([(self.audio_ipc, "volume", vol, 0)], fade)
        self.audio_ipc.set("volume", 0 if fade else vol)
        try:
            self.audio_ipc.load(self.library.dir / name)
            self.audio_ipc.set("pause", self.paused)
            if fade:
                self._ramp([(self.audio_ipc, "volume", 0, self._volume())], fade)
        finally:
            self._quiet(self.audio_ipc, "volume", self._volume())

    def _wait(self, done: Callable[[], Any], timeout: float = 1.5) -> None:
        end = self.clock() + timeout
        while self.clock() < end:
            if done():
                return
            self.sleep(0.05)
        log.warning("timed out waiting for the audio device to be released")

    def _quiet(self, ipc: MpvIpc, prop: str, value: Any) -> None:
        try:
            ipc.set(prop, value)
        except PlayerError:
            pass

    def _quiet_command(self, ipc: MpvIpc, *args: Any) -> None:
        try:
            ipc.command(*args)
        except PlayerError:
            pass

    # --- files that froze the frame ----------------------------------------------

    def showing(self) -> str | None:
        """The artwork on screen, or the one mpv is opening right now."""
        return self.visual.loading or self.visual.current

    def skip_visual(self, name: str, message: str) -> bool:
        """Leave ``name`` out of the playlist until the user next changes it.

        Used after ``name`` froze the picture or hung the graphics. Not done when
        it's the only artwork: retrying it beats a black screen.
        """
        visuals, _ = self._wanted()
        if name not in visuals or len(visuals) < 2:
            return False
        self.visual.items = visuals
        self.visual.failed.add(name)
        if self.visual.current == name:
            self.visual.current = self._next(self.visual, False)
        self._note_error(message)
        return True

    def _note_error(self, message: str) -> None:
        self.error = message
        self.changed = True
        if message != self._last_logged:  # don't repeat the same error every tick
            log.error("%s", message)
            self._last_logged = message

    # --- status -----------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        def channel(ch: Channel) -> dict[str, Any]:
            items = ch.playable()
            next_in = None
            if len(items) >= 2 and ch.length is not None and not self.paused:
                next_in = round(max(0.0, ch.length - self.state["fade"] - ch.elapsed), 1)
            return {
                "current": ch.current,
                "position": items.index(ch.current) + 1 if ch.current in items else None,
                "count": len(items),
                "next_at": time.time() + next_in if next_in is not None else None,
            }

        self.changed = False
        return {"visual": channel(self.visual), "sound": channel(self.sound),
                "paused": self.paused, "error": self.error}
