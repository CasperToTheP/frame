"""Talk to a real mpv over IPC (no display: video/audio go to null outputs).

Skipped unless both ``mpv`` and ``ffmpeg`` are on PATH and unix sockets exist,
so it never runs on Windows or in the default CI job. On a dev box or the Pi:

    pytest tests/test_mpv_integration.py -v
"""

import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest

from frame.player import InvalidMedia, MpvIpc, build_mpv_args
from frame.state import DEFAULTS

pytestmark = pytest.mark.skipif(
    not (shutil.which("mpv") and shutil.which("ffmpeg") and hasattr(socket, "AF_UNIX")),
    reason="needs mpv, ffmpeg and unix sockets",
)


def make_clip(path: Path, seconds: float = 2) -> None:
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y",
         "-f", "lavfi", "-i", f"testsrc2=size=320x180:rate=30:duration={seconds}",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)],
        check=True,
    )


@pytest.fixture
def mpv(tmp_path):
    a = tmp_path / "a.mp4"
    b = tmp_path / "b.mp4"
    make_clip(a)
    make_clip(b)
    sock = tmp_path / "mpv.sock"
    state = dict(DEFAULTS, volume=40, rotation=90)
    args = build_mpv_args("mpv", sock, state, a)
    # Same flags as on the Pi, except headless outputs.
    args = [x for x in args if not x.startswith(("--vo=", "--gpu-context=", "--ao="))]
    args[1:1] = ["--vo=null", "--ao=null"]
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    ipc = MpvIpc(sock)
    for _ in range(100):
        if proc.poll() is not None:
            pytest.fail("mpv exited at startup:\n" + proc.stdout.read().decode())
        if sock.exists() and ipc.ping():
            break
        time.sleep(0.05)
    yield ipc, tmp_path
    proc.terminate()
    proc.wait(5)


def wait_for(fn, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = fn()
        if value:
            return value
        time.sleep(0.05)
    return fn()


def test_startup_flags_applied(mpv):
    ipc, tmp = mpv
    assert wait_for(lambda: ipc.get("path")) == str(tmp / "a.mp4")
    assert ipc.get("volume") == 40
    assert ipc.get("video-rotate") == 90
    assert ipc.get("loop-file") in ("inf", True)


def test_loadfile_pause_volume_and_live_settings(mpv):
    ipc, tmp = mpv
    ipc.load(tmp / "b.mp4")
    assert ipc.get("path") == str(tmp / "b.mp4")
    ipc.set("pause", True)
    assert ipc.get("pause") is True
    ipc.set("pause", False)
    ipc.set("volume", 65)
    assert ipc.get("volume") == 65
    ipc.set("mute", True)
    assert ipc.get("mute") is True
    for prop, value in [("video-rotate", 270), ("keepaspect", False), ("panscan", 1.0),
                        ("hwdec", "auto-safe"), ("audio-device", "auto")]:
        ipc.set(prop, value)
    assert ipc.get("video-rotate") == 270


def test_loops_instead_of_ending(mpv):
    ipc, tmp = mpv
    # The 2 s clip must still be playing well after its end.
    time.sleep(3.5)
    assert ipc.get("path") == str(tmp / "a.mp4")
    assert ipc.get("idle-active") is False


def test_corrupt_file_reported_and_player_survives(mpv):
    ipc, tmp = mpv
    bad = tmp / "bad.mp4"
    bad.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"garbage" * 100)
    with pytest.raises(InvalidMedia):
        ipc.load(bad)
    assert ipc.ping()
    ipc.load(tmp / "a.mp4")
    assert ipc.get("path") == str(tmp / "a.mp4")


def test_stop_goes_idle(mpv):
    ipc, _ = mpv
    ipc.command("stop")
    assert wait_for(lambda: ipc.get("idle-active")) is True
