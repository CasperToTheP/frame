import subprocess
import sys
import time

import pytest

from frame import player_service
from frame.player_service import MAX_BACKOFF, MIN_BACKOFF, Supervisor, next_backoff
from frame.state import StateStore

from .conftest import MP4


@pytest.fixture(autouse=True)
def mpv_035(monkeypatch):
    # Don't run a real "mpv --version"; pretend to be Bookworm's mpv.
    monkeypatch.setattr(player_service, "mpv_version", lambda mpv_bin: (0, 35))


def test_backoff_grows_and_caps():
    b = MIN_BACKOFF
    seen = []
    for _ in range(10):
        b = next_backoff(b, ran_for=1)
        seen.append(b)
    assert seen[0] == 2 and seen[1] == 4
    assert max(seen) == MAX_BACKOFF


def test_backoff_resets_after_healthy_run():
    assert next_backoff(MAX_BACKOFF, ran_for=3600) == MIN_BACKOFF


class FakePopen:
    instances = []

    def __init__(self, args, **kw):
        self.args = args
        self.pid = 4242
        self.returncode = None
        FakePopen.instances.append(self)

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = -15

    def kill(self):
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


def test_start_mpv_uses_saved_state_and_display(cfg, monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    cfg.media_dir.mkdir(parents=True)
    (cfg.media_dir / "a.mp4").write_bytes(MP4)
    StateStore(cfg.state_file).update(playlist=["a.mp4"], volume=12, rotation=180)
    conn = cfg.sys_drm / "card1-HDMI-A-1"
    conn.mkdir(parents=True)
    (conn / "status").write_text("connected\n")
    (cfg.proc_asound / "vc4hdmi0").mkdir(parents=True)

    sup = Supervisor(cfg)
    has_sysfs, connector, _ = sup._display_snapshot()
    assert has_sysfs and connector.name == "HDMI-A-1"
    sup.start_mpv(connector)
    args, audio_args = FakePopen.instances[-2].args, FakePopen.instances[-1].args
    assert "--volume=12" in args and "--video-rotate=180" in args
    assert "--drm-device=/dev/dri/card1" in args
    assert "--audio-device=alsa/hdmi:CARD=vc4hdmi0,DEV=0" in args
    assert args[-1].endswith("a.mp4")
    assert "--aid=no" not in args
    assert "--alpha=blend" in args
    # The audio-only player idles: no soundtrack chosen.
    assert "--no-video" in audio_args and "--" not in audio_args
    assert "--audio-device=alsa/hdmi:CARD=vc4hdmi0,DEV=0" in audio_args
    assert cfg.player_status_file.exists()


def test_start_mpv_with_soundtrack(cfg, monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    cfg.media_dir.mkdir(parents=True)
    (cfg.media_dir / "a.png").write_bytes(MP4)
    (cfg.media_dir / "song.mp3").write_bytes(MP4)
    StateStore(cfg.state_file).update(playlist=["a.png"], sounds=["song.mp3"], volume=33)
    Supervisor(cfg).start_mpv(None)
    args, audio_args = FakePopen.instances[-2].args, FakePopen.instances[-1].args
    assert "--aid=no" in args
    assert audio_args[-1].endswith("song.mp3") and "--volume=33" in audio_args


def test_watch_restarts_pair_when_audio_player_dies(cfg, monkeypatch):
    sup, stopped = running_supervisor(cfg, monkeypatch, lambda: None)
    sup.audio_proc = FakePopen(["mpv"])
    sup.audio_proc.returncode = 1
    assert sup.watch(frozenset()) == "audio player exited with code 1"
    assert stopped == ["audio player exited"]


def test_start_mpv_with_missing_artwork_shows_black(cfg, monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    StateStore(cfg.state_file).update(playlist=["gone.mp4"])
    Supervisor(cfg).start_mpv(None)
    assert "--" not in FakePopen.instances[-2].args


def test_restarts_mpv_after_crash(cfg):
    """End to end with a fake 'mpv' that exits immediately."""
    script = cfg.data_dir / "fake_mpv.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("import sys; sys.exit(3)\n")
    # Run the fake through the current Python interpreter.
    cfg = type(cfg)(**{**cfg.__dict__, "mpv_bin": sys.executable})
    sup = Supervisor(cfg)

    real_build = player_service.build_mpv_args
    real_audio = player_service.build_audio_args

    def build(*a, **kw):
        return [sys.executable, str(script)] + real_build(*a, **kw)[1:2]

    player_service.build_mpv_args = build
    # The audio player just stays up.
    player_service.build_audio_args = lambda *a, **kw: [
        sys.executable, "-c", "import time; time.sleep(30)"]
    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        time.sleep(0.01)
        if sup.restarts >= 3:
            sup.stopping = True

    sup._sleep = fake_sleep
    try:
        sup.run()
    finally:
        player_service.build_mpv_args = real_build
        player_service.build_audio_args = real_audio
    assert sup.restarts >= 2
    assert sup.last_exit == 3
    assert any(s >= 4 for s in sleeps)  # backoff grew


def running_supervisor(cfg, monkeypatch, health):
    monkeypatch.setattr(player_service, "HEALTH_EVERY", 0)
    monkeypatch.setattr(player_service, "STARTUP_GRACE", 0)
    sup = Supervisor(cfg)
    sup.proc = FakePopen(["mpv"])
    sup.started_at = time.monotonic()
    sup._sleep = lambda s: None
    sup.check_health = health
    stopped = []
    sup.stop_mpv = lambda reason: stopped.append(reason)
    return sup, stopped


def test_watch_restarts_unhealthy_mpv(cfg, monkeypatch):
    sup, stopped = running_supervisor(cfg, monkeypatch, lambda: "video output not running")
    reason = sup.watch(frozenset())
    assert reason.startswith("unhealthy")
    assert len(stopped) == 1


def test_watch_tolerates_transient_health_failures(cfg, monkeypatch):
    results = iter(["slow", "slow", None, "slow", "slow", None])
    calls = []

    def health():
        calls.append(1)
        try:
            return next(results)
        except StopIteration:
            sup.stopping = True
            return None

    sup, stopped = running_supervisor(cfg, monkeypatch, health)
    assert sup.watch(frozenset()) is None
    assert stopped == []


def test_watch_restarts_on_display_reconnect(cfg, monkeypatch):
    conn = cfg.sys_drm / "card1-HDMI-A-1"
    conn.mkdir(parents=True)
    (conn / "status").write_text("connected\n")
    sup, stopped = running_supervisor(cfg, monkeypatch, lambda: None)
    # Previously nothing was connected; now HDMI-A-1 is.
    assert sup.watch(frozenset()) == "display reconnected"
    assert stopped == ["display reconnected"]


def test_watch_skips_health_check_while_display_unplugged(cfg, monkeypatch):
    conn = cfg.sys_drm / "card1-HDMI-A-1"
    conn.mkdir(parents=True)
    (conn / "status").write_text("disconnected\n")
    checks = []

    def health():
        checks.append(1)
        return "broken"

    sup, stopped = running_supervisor(cfg, monkeypatch, health)
    n = iter(range(5))
    sup._sleep = lambda s: next(n, None) is None and setattr(sup, "stopping", True)
    assert sup.watch(frozenset({"card1-HDMI-A-1"})) is None
    assert checks == [] and stopped == []
