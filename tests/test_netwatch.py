"""The Wi-Fi fallback, with a fake NetworkManager and a fake clock."""

import subprocess

import pytest

from frame import netwatch
from frame.netwatch import (
    HOTSPOT,
    IDLE_BEFORE_RETRY,
    RETRY_EVERY,
    START_AFTER,
    TRY_FOR,
    NetWatch,
    Nmcli,
    read_status,
)


class FakeNm:
    def __init__(self, home=("Home",), reachable=False):
        self.home = list(home)
        self.reachable = reachable  # can the home Wi-Fi be joined right now?
        self.active = None
        self.phones = 0
        self.calls = []
        self.fail_up = False

    def wifi_device(self):
        return "wlan0", self.active

    def home_networks(self):
        return self.home

    def hotspot_exists(self):
        return True

    def up(self, name):
        self.calls.append(("up", name))
        if self.fail_up:
            raise OSError("Error: Connection activation failed")
        self.active = name

    def down(self, name):
        self.calls.append(("down", name))
        self.active = None

    def stations(self, device):
        return self.phones


class Clock:
    t = 0.0

    def __call__(self):
        return self.t


@pytest.fixture
def env(cfg):
    nm, clock = FakeNm(), Clock()
    watch = NetWatch(cfg, nm, clock)

    def run(seconds, step=10, join_home=True):
        end = clock.t + seconds
        while clock.t < end:
            clock.t += step
            # NetworkManager joins the home network by itself when Wi-Fi is free.
            if join_home and nm.active is None and nm.reachable and nm.home:
                nm.active = nm.home[0]
            watch.tick()

    return nm, watch, run, cfg


def test_home_wifi_means_no_hotspot(env):
    nm, watch, run, cfg = env
    nm.reachable = True
    run(3600)
    assert watch.mode == "wifi" and nm.calls == []
    assert read_status(cfg.network_status_file) == {"mode": "wifi", "network": "Home"}


def test_hotspot_starts_when_home_wifi_is_missing(env):
    nm, watch, run, cfg = env
    run(START_AFTER - 10)
    assert nm.active is None and watch.mode == "offline"
    run(20)
    assert nm.active == HOTSPOT
    assert read_status(cfg.network_status_file) == {"mode": "hotspot", "network": "Frame"}


def test_hotspot_starts_after_losing_the_connection(env):
    nm, watch, run, _ = env
    nm.reachable = True
    run(60)
    nm.reachable, nm.active = False, None  # signal gone
    run(START_AFTER + 10)
    assert nm.active == HOTSPOT


def test_retries_home_wifi_only_while_nobody_uses_the_hotspot(env):
    nm, watch, run, _ = env
    run(START_AFTER + 10)
    nm.phones = 1
    run(RETRY_EVERY * 2)
    assert ("down", HOTSPOT) not in nm.calls  # a phone is connected: leave it alone
    nm.phones = 0
    run(IDLE_BEFORE_RETRY - 20)
    assert ("down", HOTSPOT) not in nm.calls
    nm.reachable = True
    run(40)
    assert ("down", HOTSPOT) in nm.calls
    assert watch.mode == "wifi" and nm.active == "Home"


def test_hotspot_comes_back_when_home_wifi_still_fails(env):
    nm, watch, run, _ = env
    run(START_AFTER + 10)
    run(RETRY_EVERY + 10)
    assert watch.mode == "trying home wifi" and nm.active is None
    run(TRY_FOR + 10)
    assert nm.active == HOTSPOT and nm.calls.count(("up", HOTSPOT)) == 2


def test_no_saved_home_wifi_means_hotspot_for_good(env):
    nm, watch, run, _ = env
    nm.home = []
    run(START_AFTER + RETRY_EVERY * 3)
    assert nm.active == HOTSPOT and ("down", HOTSPOT) not in nm.calls


def test_failed_hotspot_is_retried_slowly(env):
    nm, watch, run, _ = env
    nm.fail_up = True
    run(START_AFTER * 3 + 5)
    assert 2 <= nm.calls.count(("up", HOTSPOT)) <= 3


def test_nmcli_parsing():
    outputs = {
        "device": "p2p-dev-wlan0:wifi-p2p:disconnected:\neth0:ethernet:unavailable:\n"
                  "wlan0:wifi:connected:My Home\\: 5G\nlo:loopback:connected (externally):lo\n",
        "connection": "My Home\\: 5G:802-11-wireless\nframe-hotspot:802-11-wireless\n"
                      "lo:loopback\nWired connection 1:802-3-ethernet\n",
    }

    def run(args, **kw):
        if args[0] == "iw":
            out = "Station aa:bb (on wlan0)\n\tinactive time: 10 ms\nStation cc:dd (on wlan0)\n"
            return subprocess.CompletedProcess(args, 0, out, "")
        key = "device" if "device" in args else "connection"
        out = outputs[key]
        if args[-3:] == ["NAME", "connection", "show"]:
            out = "".join(line.rpartition(":")[0] + "\n" for line in out.splitlines())
        return subprocess.CompletedProcess(args, 0, out, "")

    nm = Nmcli(run)
    assert nm.wifi_device() == ("wlan0", "My Home: 5G")
    assert nm.home_networks() == ["My Home: 5G"]
    assert nm.hotspot_exists()
    assert nm.stations("wlan0") == 2
    outputs["device"] = "wlan0:wifi:disconnected:\n"
    assert nm.wifi_device() == ("wlan0", None)


def test_nmcli_errors_are_raised(monkeypatch):
    nm = Nmcli(lambda args, **kw: subprocess.CompletedProcess(args, 8, "", "Error: no NM"))
    with pytest.raises(OSError, match="no NM"):
        nm.up(HOTSPOT)


def test_offline_without_wifi_hardware(cfg):
    class NoWifi(FakeNm):
        def wifi_device(self):
            return None, None

    watch = NetWatch(cfg, NoWifi(), Clock())
    watch.tick()
    assert watch.mode == "offline"


def test_status_file_written_only_on_change(env, monkeypatch):
    nm, watch, run, _ = env
    writes = []
    monkeypatch.setattr(netwatch, "write_status", lambda *a: writes.append(a))
    nm.reachable = True
    run(600)
    assert len(writes) == 1
