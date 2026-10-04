"""frame-player.service: keep exactly one fullscreen mpv running, forever.

A second, audio-only mpv runs next to it for separate soundtracks. The two are
supervised as a pair: if either fails, both are restarted. The Director
(director.py) steps through the playlists and fades between items.

Responsibilities:
  * start mpv with the saved artwork/volume/rotation (read from state.json)
  * restart mpv if it exits, with exponential backoff (no tight crash loops)
  * wait for an HDMI display before starting, and restart mpv when the display
    is reconnected so the video mode and HDMI audio are set up again
  * publish a tiny health file at /run/frame/player.json for the web UI
  * restart both players if a video's picture stops moving (freeze watchdog)
  * reboot the Pi if mpv gets stuck in the kernel, which happens when the
    VideoCore graphics firmware hangs: nothing short of a reboot recovers that

It deliberately does not import Flask or talk to the web service: playback must
keep working even if the web UI is down.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import display
from .config import Config, setup_logging
from .director import Director
from .media import kind_of
from .player import (
    MpvIpc,
    PlayerError,
    PlayerUnavailable,
    build_audio_args,
    build_mpv_args,
    mpv_version,
)

log = logging.getLogger("frame.player")

# Often enough for playlist timing and fades to feel exact.
POLL_SECONDS = 0.5
MIN_BACKOFF = 1.0
MAX_BACKOFF = 60.0
# A run longer than this counts as healthy and resets the backoff.
HEALTHY_RUN = 60.0
# mpv with --idle does not exit when it can't open the display or hangs, so it
# is also checked over IPC. Three failed checks in a row => restart it.
HEALTH_EVERY = 10.0
STARTUP_GRACE = 15.0
MAX_HEALTH_FAILURES = 3
# Freeze watchdog: a playing video or GIF whose position hasn't moved for this
# long (while not paused) has a frozen picture.
FREEZE_SECONDS = 15.0
FREEZE_CHECK_EVERY = 2.5
# Shorter files are left alone: a one-frame "video" never moves.
FREEZE_MIN_DURATION = 1.0
# After SIGKILL, a process that still hasn't exited is stuck in the kernel.
KILL_WAIT = 20.0
# Exit code that asks systemd to reboot (ExecStopPost in frame-player.service).
REBOOT_EXIT_CODE = 75
# Never reboot more often than this, so a fault that comes straight back can't
# turn into a reboot loop. Over the limit, the supervisor keeps retrying instead.
MAX_REBOOTS = 3
REBOOT_WINDOW = 6 * 3600


def next_backoff(current: float, ran_for: float) -> float:
    if ran_for >= HEALTHY_RUN:
        return MIN_BACKOFF
    return min(MAX_BACKOFF, max(MIN_BACKOFF, current * 2))


class Supervisor:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.proc: subprocess.Popen | None = None
        self.audio_proc: subprocess.Popen | None = None
        self.mpv_version: tuple[int, int] | None = None
        self.started_at = 0.0
        self.backoff = MIN_BACKOFF
        self.restarts = 0
        self.last_exit: int | None = None
        self.connector: display.Connector | None = None
        self.audio_device: str | None = None
        self.stopping = False
        self._waiting_logged = False
        # PIDs of players that could not be stopped (stuck in the kernel).
        self.stuck: list[int] = []
        self.ipc = MpvIpc(cfg.mpv_socket, timeout=3.0)
        self.audio_ipc = MpvIpc(cfg.audio_socket, timeout=3.0)
        self.director = Director(cfg, self.ipc, self.audio_ipc, sleep=self._sleep)

    def check_health(self) -> str | None:
        """None if mpv is healthy, otherwise a description of the problem."""
        try:
            # True whenever mpv has a working video output, even while idle
            # (thanks to --force-window). False if DRM/KMS setup failed.
            if self.ipc.get("vo-configured", False) is not True:
                return "video output not running (display could not be opened)"
        except PlayerUnavailable as exc:
            return f"not answering on IPC ({exc})"
        if self.audio_proc is not None and not self.audio_ipc.ping():
            return "audio player not answering on IPC"
        return None

    def playback_position(self) -> float | None:
        """Position of a playing video or GIF, or None when the freeze watchdog
        doesn't apply (paused, idle, an image, a very short file)."""
        if kind_of(self.director.visual.current or "") not in ("video", "animation"):
            return None
        try:
            if self.ipc.get("pause", False) or self.ipc.get("idle-active", True):
                return None
            duration = self.ipc.get("duration")
            if not isinstance(duration, (int, float)) or duration < FREEZE_MIN_DURATION:
                return None
            pos = self.ipc.get("time-pos")
        except PlayerError:
            return None  # not answering: that's the health check's job
        return float(pos) if isinstance(pos, (int, float)) else None

    # --- graphics hangs -----------------------------------------------------

    @property
    def hang_file(self) -> Path:
        return self.cfg.data_dir / "hang.json"

    def skip_after_hang(self) -> None:
        """After a hang reboot, leave out the file that was playing, unless the
        user has changed the playlist since."""
        record = _read_json(self.hang_file)
        name, at = record.get("file"), record.get("at")
        if not isinstance(name, str) or not isinstance(at, (int, float)):
            return
        try:
            if self.cfg.state_file.stat().st_mtime > at:
                return
        except OSError:
            pass
        self.director.skip_visual(
            name, f"{name} was skipped: the frame hung while playing it and restarted. "
                  "It is probably too heavy for the Pi (see README, Preparing artwork).")

    def handle_stuck(self) -> bool:
        """Players are stuck in the kernel. True if the Pi should reboot now."""
        name = self.director.showing()
        log.error("mpv is stuck in the kernel (pid %s)%s. The graphics firmware has "
                  "probably hung; only a reboot recovers.",
                  ", ".join(map(str, self.stuck)), f" while playing {name}" if name else "")
        self.stuck.clear()
        record = _read_json(self.hang_file)
        now = time.time()
        # A clock that went backwards (no RTC, no network) still counts as recent.
        recent = [t for t in record.get("reboots", [])
                  if isinstance(t, (int, float)) and now - t < REBOOT_WINDOW]
        if len(recent) >= MAX_REBOOTS:
            log.error("not rebooting: already rebooted %d times in the last %d hours; "
                      "retrying the player instead", len(recent), REBOOT_WINDOW // 3600)
            return False
        if not _write_json(self.hang_file, {"reboots": recent + [now], "file": name, "at": now}):
            return False
        log.error("rebooting to recover")
        return True

    # --- display ------------------------------------------------------------

    def _display_snapshot(self) -> tuple[bool, display.Connector | None, frozenset]:
        """(sysfs_available, chosen_connector, set of connected connector names)."""
        connectors = display.list_connectors(self.cfg.sys_drm)
        if not connectors:
            # Not a KMS system (dev machine) or sysfs unreadable: don't gate on it.
            return False, None, frozenset()
        connected = frozenset(f"{c.card}-{c.name}" for c in connectors if c.connected)
        return True, display.pick_connector(connectors), connected

    # --- mpv lifecycle ------------------------------------------------------

    def start_mpv(self, connector: display.Connector | None) -> None:
        media_path, soundtrack = self.director.initial()
        state = self.director.state

        audio = state.get("audio_device") or "auto"
        if audio == "auto":
            audio = display.hdmi_audio_device(connector, self.cfg.proc_asound)
        self.audio_device = audio
        self.connector = connector
        if self.mpv_version is None:
            self.mpv_version = mpv_version(self.cfg.mpv_bin)

        args = build_mpv_args(
            self.cfg.mpv_bin,
            self.cfg.mpv_socket,
            state,
            media_path,
            drm_device=connector.device if connector else None,
            drm_connector=connector.name if connector else None,
            audio_device=audio,
            mpv_version=self.mpv_version,
            own_audio=soundtrack is None,
        )
        audio_args = build_audio_args(
            self.cfg.mpv_bin, self.cfg.audio_socket, state, soundtrack, audio_device=audio
        )
        for sock in (self.cfg.mpv_socket, self.cfg.audio_socket):
            try:
                sock.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                log.warning("could not remove stale socket: %s", exc)
        log.info(
            "starting mpv: display=%s audio=%s artwork=%s soundtrack=%s",
            f"{connector.card}/{connector.name}" if connector else "default",
            audio or "default",
            media_path.name if media_path else "(none)",
            soundtrack.name if soundtrack else "(artwork's own)",
        )
        log.debug("mpv command: %s", " ".join(args))
        self.proc = subprocess.Popen(args, stdin=subprocess.DEVNULL)
        self.started_at = time.monotonic()
        try:
            self.audio_proc = subprocess.Popen(audio_args, stdin=subprocess.DEVNULL)
        except OSError:
            self.stop_mpv("audio player could not start")
            raise
        self.write_status()

    def stop_mpv(self, reason: str) -> None:
        if self.proc and self.proc.poll() is None:
            log.info("stopping mpv (%s)", reason)
        for proc, sock in ((self.proc, self.cfg.mpv_socket),
                           (self.audio_proc, self.cfg.audio_socket)):
            if proc is not None and not _stop_process(proc, sock):
                self.stuck.append(proc.pid)

    def write_status(self) -> None:
        running = bool(self.proc and self.proc.poll() is None)
        audio_running = bool(self.audio_proc and self.audio_proc.poll() is None)
        status = {
            "supervisor_pid": os.getpid(),
            "mpv_pid": self.proc.pid if running else None,
            "mpv_running": running,
            "audio_mpv_running": audio_running,
            "mpv_started_at": time.time() - (time.monotonic() - self.started_at)
            if running
            else None,
            "restarts": self.restarts,
            "last_exit_code": self.last_exit,
            "display": f"{self.connector.card}-{self.connector.name}" if self.connector else None,
            "audio_device": self.audio_device,
            "playlist": self.director.status(),
            "updated_at": time.time(),
        }
        path = self.cfg.player_status_file
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(status), encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:
            log.debug("could not write player status: %s", exc)

    # --- main loop ----------------------------------------------------------

    def run(self) -> int:
        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGINT, self._on_signal)
        log.info("frame player starting (pid %d)", os.getpid())

        self.skip_after_hang()
        has_sysfs, connector, connected = self._display_snapshot()
        while not self.stopping:
            # 1. Wait for a display if the system can tell us about displays.
            if has_sysfs and connector is None:
                if not self._waiting_logged:
                    log.warning("no HDMI display connected; waiting for one")
                    self._waiting_logged = True
                    self.write_status()
                self._sleep(POLL_SECONDS)
                has_sysfs, connector, connected = self._display_snapshot()
                continue
            if self._waiting_logged:
                log.info("display connected: %s", connector.name if connector else "?")
                self._waiting_logged = False

            # 2. Run mpv and watch it.
            try:
                self.start_mpv(connector)
            except OSError as exc:
                log.error("could not start mpv (%s): %s", self.cfg.mpv_bin, exc)
                self._sleep(MAX_BACKOFF)
                continue

            reason = self.watch(connected)
            if self.stopping:
                break
            if self.stuck and self.handle_stuck():
                self.write_status()
                return REBOOT_EXIT_CODE
            ran_for = time.monotonic() - self.started_at
            if reason == "display reconnected":
                delay = MIN_BACKOFF
            else:
                self.backoff = next_backoff(self.backoff, ran_for)
                delay = self.backoff
                log.error("mpv %s after %.0fs; restarting in %.0fs", reason, ran_for, delay)
            self.restarts += 1
            self.write_status()
            self._sleep(delay)
            has_sysfs, connector, connected = self._display_snapshot()

        self.stop_mpv("service stopping")
        self.write_status()
        log.info("frame player stopped")
        return 0

    def watch(self, connected: frozenset) -> str | None:
        """Block while mpv runs fine. Returns why it needs restarting (None if stopping)."""
        failures = 0
        last_check = time.monotonic()
        last_freeze_check = 0.0
        position, moved_at = None, time.monotonic()
        while not self.stopping:
            code = self.proc.poll()
            if code is not None:
                self.last_exit = code
                self.stop_mpv("main player exited")
                return f"exited with code {code}"
            if self.audio_proc is not None:
                code = self.audio_proc.poll()
                if code is not None:
                    self.last_exit = code
                    self.stop_mpv("audio player exited")
                    return f"audio player exited with code {code}"
            self._sleep(POLL_SECONDS)
            if self.stopping:
                break
            self.director.tick()
            if self.director.changed:
                self.write_status()

            has_sysfs, _, now_connected = self._display_snapshot()
            if has_sysfs and (now_connected - connected):
                # A display was (re)connected: redo mode setting and HDMI audio.
                log.info("display hotplug detected (%s)", ", ".join(sorted(now_connected)))
                self.stop_mpv("display reconnected")
                return "display reconnected"
            if has_sysfs and connected and not now_connected:
                log.warning("display disconnected; mpv keeps running until it returns")
            connected = now_connected

            now = time.monotonic()
            display_present = not has_sysfs or bool(connected)
            if not display_present or now - self.started_at < STARTUP_GRACE:
                position, moved_at = None, now
                continue
            if now - last_freeze_check >= FREEZE_CHECK_EVERY:
                last_freeze_check = now
                pos = self.playback_position()
                if pos is None or pos != position:
                    position, moved_at = pos, now
                elif now - moved_at >= FREEZE_SECONDS:
                    return self._frozen(now - moved_at)
            if now - last_check >= HEALTH_EVERY:
                last_check = now
                problem = self.check_health()
                if problem is None:
                    failures = 0
                    continue
                failures += 1
                log.warning("mpv health check failed (%d/%d): %s",
                            failures, MAX_HEALTH_FAILURES, problem)
                if failures >= MAX_HEALTH_FAILURES:
                    self.stop_mpv(f"unhealthy: {problem}")
                    return f"unhealthy: {problem}"
        return None

    def _frozen(self, seconds: float) -> str:
        name = self.director.visual.current
        log.warning("picture frozen for %.0fs while playing %s; restarting the players",
                    seconds, name)
        if name:
            self.director.skip_visual(
                name, f"{name} was skipped: its picture froze. It is probably too heavy "
                      "for the Pi (see README, Preparing artwork).")
        self.stop_mpv(f"picture frozen: {name}")
        return f"picture frozen ({name})"

    def _sleep(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while not self.stopping and time.monotonic() < end:
            time.sleep(min(0.25, end - time.monotonic()))

    def _on_signal(self, signum, _frame) -> None:
        log.info("received signal %d", signum)
        self.stopping = True


def _stop_process(proc: subprocess.Popen, sock) -> bool:
    """Stop one mpv. False if it can't be stopped (stuck in the kernel)."""
    if proc.poll() is not None:
        return True
    # Ask nicely via IPC first so mpv releases DRM and audio cleanly.
    try:
        MpvIpc(sock, timeout=1.0).command("quit")
    except Exception:
        proc.terminate()
    try:
        proc.wait(timeout=5)
        return True
    except subprocess.TimeoutExpired:
        log.warning("mpv did not exit; killing it")
    proc.kill()
    # Never wait without a limit: a process blocked in a driver ignores even
    # SIGKILL, and waiting for it would stop the supervisor for good.
    try:
        proc.wait(timeout=KILL_WAIT)
        return True
    except subprocess.TimeoutExpired:
        log.error("mpv (pid %d) did not exit %.0fs after SIGKILL", proc.pid, KILL_WAIT)
        return False


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, data: dict) -> bool:
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())  # the Pi reboots right after this
        os.replace(tmp, path)
        return True
    except OSError as exc:
        log.error("could not write %s: %s", path, exc)
        return False


def main() -> int:
    setup_logging()
    return Supervisor(Config.from_env()).run()


if __name__ == "__main__":
    sys.exit(main())
