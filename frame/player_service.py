"""frame-player.service: keep exactly one fullscreen mpv running, forever.

Responsibilities:
  * start mpv with the saved artwork/volume/rotation (read from state.json)
  * restart mpv if it exits, with exponential backoff (no tight crash loops)
  * wait for an HDMI display before starting, and restart mpv when the display
    is reconnected so the video mode and HDMI audio are set up again
  * publish a tiny health file at /run/frame/player.json for the web UI

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

from . import display
from .config import Config, setup_logging
from .media import MediaLibrary
from .player import MpvIpc, PlayerUnavailable, build_mpv_args
from .state import StateStore

log = logging.getLogger("frame.player")

POLL_SECONDS = 2.0
MIN_BACKOFF = 1.0
MAX_BACKOFF = 60.0
# A run longer than this counts as healthy and resets the backoff.
HEALTHY_RUN = 60.0
# mpv with --idle does not exit when it can't open the display or hangs, so it
# is also checked over IPC. Three failed checks in a row => restart it.
HEALTH_EVERY = 10.0
STARTUP_GRACE = 15.0
MAX_HEALTH_FAILURES = 3


def next_backoff(current: float, ran_for: float) -> float:
    if ran_for >= HEALTHY_RUN:
        return MIN_BACKOFF
    return min(MAX_BACKOFF, max(MIN_BACKOFF, current * 2))


class Supervisor:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.state = StateStore(cfg.state_file)
        self.library = MediaLibrary(cfg.media_dir, cfg.incoming_dir)
        self.proc: subprocess.Popen | None = None
        self.started_at = 0.0
        self.backoff = MIN_BACKOFF
        self.restarts = 0
        self.last_exit: int | None = None
        self.connector: display.Connector | None = None
        self.audio_device: str | None = None
        self.stopping = False
        self._waiting_logged = False
        self.ipc = MpvIpc(cfg.mpv_socket, timeout=3.0)

    def check_health(self) -> str | None:
        """None if mpv is healthy, otherwise a description of the problem."""
        try:
            # True whenever mpv has a working video output, even while idle
            # (thanks to --force-window). False if DRM/KMS setup failed.
            if self.ipc.get("vo-configured", False) is not True:
                return "video output not running (display could not be opened)"
        except PlayerUnavailable as exc:
            return f"not answering on IPC ({exc})"
        return None

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
        state = self.state.load()
        current = state.get("current")
        media_path = None
        if current:
            if self.library.exists(current):
                media_path = self.library.resolve(current)
            else:
                log.warning("saved artwork %r is missing; showing black screen", current)

        audio = state.get("audio_device") or "auto"
        if audio == "auto":
            audio = display.hdmi_audio_device(connector, self.cfg.proc_asound)
        self.audio_device = audio
        self.connector = connector

        args = build_mpv_args(
            self.cfg.mpv_bin,
            self.cfg.mpv_socket,
            state,
            media_path,
            drm_device=connector.device if connector else None,
            drm_connector=connector.name if connector else None,
            audio_device=audio,
        )
        try:
            self.cfg.mpv_socket.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.warning("could not remove stale socket: %s", exc)
        log.info(
            "starting mpv: display=%s audio=%s artwork=%s",
            f"{connector.card}/{connector.name}" if connector else "default",
            audio or "default",
            current if media_path else "(none)",
        )
        log.debug("mpv command: %s", " ".join(args))
        self.proc = subprocess.Popen(args, stdin=subprocess.DEVNULL)
        self.started_at = time.monotonic()
        self.write_status()

    def stop_mpv(self, reason: str) -> None:
        if not self.proc or self.proc.poll() is not None:
            return
        log.info("stopping mpv (%s)", reason)
        # Ask nicely via IPC first so mpv releases DRM and audio cleanly.
        try:
            MpvIpc(self.cfg.mpv_socket, timeout=1.0).command("quit")
        except Exception:
            self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            log.warning("mpv did not exit; killing it")
            self.proc.kill()
            self.proc.wait()

    def write_status(self) -> None:
        running = bool(self.proc and self.proc.poll() is None)
        status = {
            "supervisor_pid": os.getpid(),
            "mpv_pid": self.proc.pid if running else None,
            "mpv_running": running,
            "mpv_started_at": time.time() - (time.monotonic() - self.started_at)
            if running
            else None,
            "restarts": self.restarts,
            "last_exit_code": self.last_exit,
            "display": f"{self.connector.card}-{self.connector.name}" if self.connector else None,
            "audio_device": self.audio_device,
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
        while not self.stopping:
            code = self.proc.poll()
            if code is not None:
                self.last_exit = code
                return f"exited with code {code}"
            self._sleep(POLL_SECONDS)

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
            if (display_present and now - self.started_at >= STARTUP_GRACE
                    and now - last_check >= HEALTH_EVERY):
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

    def _sleep(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while not self.stopping and time.monotonic() < end:
            time.sleep(min(0.25, end - time.monotonic()))

    def _on_signal(self, signum, _frame) -> None:
        log.info("received signal %d", signum)
        self.stopping = True


def main() -> int:
    setup_logging()
    return Supervisor(Config.from_env()).run()


if __name__ == "__main__":
    sys.exit(main())
