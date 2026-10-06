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


def test_director_with_real_players(tmp_path):
    """Playlists, fades and soundtrack hand-over with two real mpv processes."""
    from frame.config import Config
    from frame.director import Director
    from frame.state import StateStore

    cfg = Config(data_dir=tmp_path / "data", run_dir=tmp_path / "run")
    cfg.media_dir.mkdir(parents=True)
    cfg.run_dir.mkdir(parents=True)
    make_clip(cfg.media_dir / "a.mp4", seconds=1)
    ffmpeg("-f", "lavfi", "-i", "testsrc2=size=64x48:rate=1:duration=1", "-frames:v", "1",
           str(cfg.media_dir / "b.png"))
    for name in ("s1.mp3", "s2.mp3"):
        ffmpeg("-f", "lavfi", "-i", "sine=frequency=330:duration=30", str(cfg.media_dir / name))
    store = StateStore(cfg.state_file)
    store.update(playlist=["a.mp4", "b.png"], interval=2, fade=0.5, volume=50)

    vproc, video = start(build_mpv_args("mpv", cfg.mpv_socket, store.load(), None,
                                        mpv_version=mpv_version("mpv")), cfg.mpv_socket)
    aproc, audio = start(build_audio_args("mpv", cfg.audio_socket, store.load(), None),
                         cfg.audio_socket)
    try:
        d = Director(cfg, video, audio)
        first, sound = d.initial()
        assert sound is None
        video.load(first)

        def run(seconds):
            end = time.monotonic() + seconds
            while time.monotonic() < end:
                d.tick()
                time.sleep(0.1)

        run(3.5)  # 2 s per item: it moved on to the image
        assert video.get("path") == str(cfg.media_dir / "b.png")
        assert video.get("brightness") == 0 and video.get("volume") == 50

        # Music from a separate list: the video's own sound goes off first.
        store.update(sounds=["s1.mp3", "s2.mp3"], sound_interval=2)
        run(0.5)
        assert video.get("aid") is False  # mpv reports aid=no as false
        assert audio.get("path") == str(cfg.media_dir / "s1.mp3")
        run(3)
        assert audio.get("path") == str(cfg.media_dir / "s2.mp3")
        assert audio.get("volume") == 50

        # Skip on request, then black screen.
        (cfg.run_dir / "next-visual").touch()
        before = video.get("path")
        run(1.5)
        assert video.get("path") != before
        store.update(blank=True)
        run(1.5)
        assert video.get("idle-active") is True and audio.get("idle-active") is True
        assert video.get("aid") == "auto"  # the next artwork gets its own sound back
    finally:
        for proc in (vproc, aproc):
            proc.terminate()
            proc.wait(5)


@pytest.mark.parametrize("name", ["v.mp4", "v.gif"])
def test_position_moves_only_while_playing(mpv, name):
    """What the freeze watchdog relies on: time-pos moves while playing (also with
    the artwork's own sound off), and stops when paused."""
    ipc, tmp = mpv
    path = tmp / name
    if name.endswith(".gif"):
        ffmpeg("-f", "lavfi", "-i", "testsrc2=size=160x90:rate=10:duration=2", str(path))
    else:
        make_clip(path)
    ipc.load(path)
    ipc.set("aid", "no")

    def position():
        for _ in range(40):
            pos = ipc.get("time-pos")
            if isinstance(pos, (int, float)):
                return pos
            time.sleep(0.05)
        pytest.fail("no time-pos")

    assert ipc.get("duration") >= 1.0
    first = position()
    time.sleep(0.6)
    assert position() != first
    ipc.set("pause", True)
    time.sleep(0.2)
    paused_at = position()
    time.sleep(0.6)
    assert position() == paused_at


def test_audio_output_can_be_reopened_while_playing(audio_mpv):
    """What the supervisor does when HDMI audio failed at start: ao-reload."""
    ipc, tmp = audio_mpv
    song = tmp / "song.mp3"
    ffmpeg("-f", "lavfi", "-i", "sine=duration=5", str(song))
    ipc.load(song)

    def audio_output():
        # The output opens shortly after the file does (and again after a reload).
        for _ in range(60):
            ao = ipc.get("current-ao")
            if ao is not None:
                return ao
            time.sleep(0.05)
        return None

    assert audio_output() == "null"  # the headless tests use --ao=null
    ipc.command("ao-reload")
    assert audio_output() == "null" and ipc.get("idle-active") is False
