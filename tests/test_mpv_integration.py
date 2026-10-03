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

from frame.media import rasterize_svg
from frame.player import InvalidMedia, MpvIpc, build_audio_args, build_mpv_args, mpv_version
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


def ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


def headless(args: list[str]) -> list[str]:
    """Same flags as on the Pi, except null video/audio outputs."""
    args = [x for x in args if not x.startswith(("--vo=", "--gpu-context=", "--ao="))]
    args[1:1] = ["--vo=null", "--ao=null"]
    return args


def start(args: list[str], sock: Path):
    proc = subprocess.Popen(headless(args), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    ipc = MpvIpc(sock)
    for _ in range(100):
        if proc.poll() is not None:
            pytest.fail("mpv exited at startup:\n" + proc.stdout.read().decode())
        if sock.exists() and ipc.ping():
            break
        time.sleep(0.05)
    return proc, ipc


@pytest.fixture
def mpv(tmp_path):
    a = tmp_path / "a.mp4"
    b = tmp_path / "b.mp4"
    make_clip(a)
    make_clip(b)
    sock = tmp_path / "mpv.sock"
    state = dict(DEFAULTS, volume=40, rotation=90, scaling="sharp")
    # The real version, so the version-specific transparency flag is checked too.
    args = build_mpv_args("mpv", sock, state, a, mpv_version=mpv_version("mpv"))
    proc, ipc = start(args, sock)
    yield ipc, tmp_path
    proc.terminate()
    proc.wait(5)


@pytest.fixture
def audio_mpv(tmp_path):
    sock = tmp_path / "audio.sock"
    proc, ipc = start(build_audio_args("mpv", sock, dict(DEFAULTS, volume=30), None), sock)
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


def test_version_specific_flags_accepted(mpv):
    ipc, _ = mpv
    assert mpv_version("mpv") is not None
    assert ipc.get("scale") == "nearest"
    ipc.set("scale", "bilinear")


@pytest.mark.parametrize("name", ["a.webp", "a.bmp", "a.tiff", "a.gif", "alpha.png"])
def test_image_formats_play(mpv, name):
    ipc, tmp = mpv
    src = "testsrc2=size=64x48:rate=10:duration=1"
    if name == "alpha.png":
        ffmpeg("-f", "lavfi", "-i", src, "-vf", "format=rgba,colorchannelmixer=aa=0.5",
               "-frames:v", "1", str(tmp / name))
    elif name == "a.gif":
        ffmpeg("-f", "lavfi", "-i", src, str(tmp / name))
    else:
        ffmpeg("-f", "lavfi", "-i", src, "-frames:v", "1", str(tmp / name))
    ipc.load(tmp / name)
    assert wait_for(lambda: ipc.get("width")) == 64


@pytest.mark.skipif(not shutil.which("rsvg-convert"), reason="needs rsvg-convert")
def test_rasterized_svg_plays(mpv):
    ipc, tmp = mpv
    svg = tmp / "a.svg"
    svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="40" height="20">'
                   '<circle cx="10" cy="10" r="8" fill="red"/></svg>')
    rasterize_svg(svg, tmp / "a.png")
    ipc.load(tmp / "a.png")
    # Rendered so the longest side is 1920 px, whatever the rsvg-convert version.
    assert wait_for(lambda: ipc.get("width")) == 1920
    assert ipc.get("height") == 960
    svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="20" height="40"/>')
    rasterize_svg(svg, tmp / "b.png")
    ipc.load(tmp / "b.png")
    assert (wait_for(lambda: ipc.get("height")), ipc.get("width")) == (1920, 960)


def test_artwork_sound_switches_off_and_on_live(mpv):
    ipc, _ = mpv
    assert wait_for(lambda: ipc.get("current-ao")) == "null"
    ipc.set("aid", "no")
    # No audio output open any more: the HDMI device is free for the soundtrack.
    assert wait_for(lambda: ipc.get("current-ao") is None) is True
    ipc.set("aid", "auto")
    assert wait_for(lambda: ipc.get("current-ao")) == "null"


@pytest.mark.parametrize("ext", ["mp3", "m4a", "aac", "wav", "flac", "ogg", "opus"])
def test_soundtrack_formats_play_and_loop(audio_mpv, ext):
    ipc, tmp = audio_mpv
    path = tmp / f"song.{ext}"
    ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:duration=1", str(path))
    assert ipc.get("idle-active") is True
    assert ipc.get("current-ao") is None  # idle: audio device closed
    ipc.load(path)
    assert ipc.get("volume") == 30
    time.sleep(1.6)  # past the end of the 1 s file
    assert ipc.get("path") == str(path)
    assert ipc.get("idle-active") is False
    ipc.command("stop")
    assert wait_for(lambda: ipc.get("idle-active")) is True
