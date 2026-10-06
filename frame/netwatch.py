"""frame-netwatch.service: fall back to the frame's own Wi-Fi hotspot.

Playback never needs the network, but the web UI does. When the Pi can't reach
the home Wi-Fi (weak signal, router gone, no network saved), it starts its own
access point so a phone can still connect: Wi-Fi "Frame", then
http://10.42.0.1:8080. While nobody uses the hotspot, it now and then hands
Wi-Fi back to NetworkManager to try the home network again.

The Pi 4's Wi-Fi chip can't reliably be a client and an access point at once,
so it is one or the other. Everything goes through ``nmcli``; the hotspot is
the NetworkManager connection the installer creates (``HOTSPOT``, autoconnect
off, so only this service turns it on). Runs as root, because switching Wi-Fi
needs it. Never imports Flask.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

from .config import Config, setup_logging

log = logging.getLogger("frame.network")

HOTSPOT = "frame-hotspot"
HOTSPOT_ADDRESS = "10.42.0.1"  # NetworkManager's address for ipv4.method=shared
POLL_SECONDS = 10
# How long without home Wi-Fi before the hotspot starts. Covers boot, where
# NetworkManager needs a while to find and join the network.
START_AFTER = 90
# While the hotspot runs with nobody connected, try the home Wi-Fi this often...
RETRY_EVERY = 10 * 60
# ...but only once the last phone left at least this long ago...
IDLE_BEFORE_RETRY = 3 * 60
# ...and give it this long to connect before the hotspot comes back.
TRY_FOR = 60

WIFI, HOTSPOT_MODE, TRYING, OFFLINE = "wifi", "hotspot", "trying home wifi", "offline"


class Nmcli:
    """The few NetworkManager facts and actions the watcher needs."""

    def __init__(self, run: Callable[..., subprocess.CompletedProcess] = subprocess.run):
        self._run = run

    def _nmcli(self, *args: str) -> str:
        res = self._run(["nmcli", "-t", *args], capture_output=True, text=True, timeout=30)
        if res.returncode != 0:
            raise OSError((res.stderr or res.stdout).strip() or f"nmcli {args[0]} failed")
        return res.stdout

    def wifi_device(self) -> tuple[str | None, str | None]:
        """(device, active connection name or None) of the first Wi-Fi device."""
        for line in self._nmcli("-f", "DEVICE,TYPE,STATE,CONNECTION", "device").splitlines():
            dev, kind, state, conn = (line.split(":", 3) + ["", "", ""])[:4]
            if kind == "wifi":
                conn = _unescape(conn) or None
                return dev, conn if state.startswith("connected") else None
        return None, None

    def home_networks(self) -> list[str]:
        """Saved Wi-Fi connections other than the hotspot."""
        out = self._nmcli("-f", "NAME,TYPE", "connection", "show")
        return [_unescape(name) for name, _, kind in (line.rpartition(":")
                                                      for line in out.splitlines())
                if kind == "802-11-wireless" and name and name != HOTSPOT]

    def hotspot_exists(self) -> bool:
        out = self._nmcli("-f", "NAME", "connection", "show")
        return HOTSPOT in out.splitlines()

    def up(self, name: str) -> None:
        self._nmcli("--wait", "20", "connection", "up", name)

    def down(self, name: str) -> None:
        self._nmcli("connection", "down", name)

    def stations(self, device: str) -> int:
        """Phones connected to the hotspot right now."""
        res = self._run(["iw", "dev", device, "station", "dump"],
                        capture_output=True, text=True, timeout=10)
        return res.stdout.count("Station ") if res.returncode == 0 else 0


class NetWatch:
    def __init__(self, cfg: Config, nm: Nmcli | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self.cfg = cfg
        self.nm = nm or Nmcli()
        self.clock = clock
        self.mode: str | None = None
        self.network: str | None = None
        self.offline_since: float | None = None
        self.hotspot_since = 0.0
        self.last_client = 0.0
        self.trying_since = 0.0
        self.stopping = False

    def tick(self) -> None:
        now = self.clock()
        device, conn = self.nm.wifi_device()
        if device is None:
            self._set(OFFLINE, None)  # no Wi-Fi hardware (or NetworkManager not running)
            return
        if conn and conn != HOTSPOT:
            self.offline_since = None
            self._set(WIFI, conn)
            return
        if conn == HOTSPOT:
            if self.mode != HOTSPOT_MODE:  # e.g. this service was restarted
                self.hotspot_since = self.last_client = now
            self._set(HOTSPOT_MODE, self.cfg.hotspot_ssid)
            if self.nm.stations(device):
                self.last_client = now
            elif (now - self.hotspot_since >= RETRY_EVERY
                  and now - self.last_client >= IDLE_BEFORE_RETRY
                  and self.nm.home_networks()):
                log.info("nobody on the hotspot; trying the home Wi-Fi again")
                self.nm.down(HOTSPOT)  # NetworkManager then joins a saved network itself
                self.trying_since = now
                self._set(TRYING, None)
            return
        # Not connected to anything.
        if self.mode == TRYING:
            if now - self.trying_since < TRY_FOR:
                return
            log.info("home Wi-Fi not reachable")
        elif self.offline_since is None:
            self.offline_since = now
            self._set(OFFLINE, None)
            if self.mode is not None:
                log.warning("Wi-Fi connection lost")
        if self.mode == TRYING or now - self.offline_since >= START_AFTER:
            self._start_hotspot(now)

    def _start_hotspot(self, now: float) -> None:
        if not self.nm.hotspot_exists():
            self._set(OFFLINE, None)
            return  # not set up by the installer: nothing to do
        log.info("starting the Wi-Fi hotspot %r (web UI at http://%s:%d)",
                 self.cfg.hotspot_ssid, HOTSPOT_ADDRESS, self.cfg.port)
        try:
            self.nm.up(HOTSPOT)
        except (OSError, subprocess.SubprocessError) as exc:
            # Try again after another START_AFTER, not on every poll.
            log.error("could not start the hotspot: %s", exc)
            self.offline_since = now
            self._set(OFFLINE, None)
            return
        self.offline_since = None
        self.hotspot_since = self.last_client = now
        self._set(HOTSPOT_MODE, self.cfg.hotspot_ssid)

    def _set(self, mode: str, network: str | None) -> None:
        if (mode, network) == (self.mode, self.network):
            return
        if mode == WIFI:
            log.info("on Wi-Fi %r", network)
        self.mode, self.network = mode, network
        write_status(self.cfg.network_status_file, mode, network)

    def run(self) -> int:
        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGINT, self._on_signal)
        log.info("network watcher starting")
        while not self.stopping:
            try:
                self.tick()
            except (OSError, subprocess.SubprocessError) as exc:
                log.warning("network check failed: %s", exc)
            end = time.monotonic() + POLL_SECONDS
            while not self.stopping and time.monotonic() < end:
                time.sleep(0.5)
        return 0

    def _on_signal(self, signum, _frame) -> None:
        self.stopping = True


def _unescape(field: str) -> str:
    # nmcli -t escapes ":" and "\\" inside fields.
    return field.replace("\\:", ":").replace("\\\\", "\\")


def write_status(path: Path, mode: str, network: str | None) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps({"mode": mode, "network": network}), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        log.debug("could not write network status: %s", exc)


def read_status(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def main() -> int:
    setup_logging()
    return NetWatch(Config.from_env()).run()


if __name__ == "__main__":
    sys.exit(main())
