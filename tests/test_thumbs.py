import os
import shutil
import subprocess
import time

import pytest

from frame.media import MediaLibrary
from frame.thumbs import Thumbnails, mpv_thumbnail_args

from .conftest import JPEG, MP3, MP4, PNG


@pytest.fixture
def lib(tmp_path):
    lib = MediaLibrary(tmp_path / "media")
    lib.ensure_dirs()
    for name, data in [("a.mp4", MP4), ("b.png", PNG), ("s.mp3", MP3)]:
        (lib.dir / name).write_bytes(data)
    return lib


def test_makes_thumbnails_for_visuals_only(lib, tmp_path, fake_thumbnailer):
    th = Thumbnails(lib, tmp_path / "thumbs")
    assert th.make("a.mp4").read_bytes() == JPEG
    assert th.make("b.png") is not None
    assert th.make("s.mp3") is None
    assert th.ready("a.mp4") and not th.ready("s.mp3")
    # Already made: mpv isn't run again.
    th.make("a.mp4")
    assert len(fake_thumbnailer) == 2


def test_args_pick_a_frame_into_videos(tmp_path):
    video = mpv_thumbnail_args("mpv", tmp_path / "a.mp4", tmp_path, "video")
    still = mpv_thumbnail_args("mpv", tmp_path / "b.png", tmp_path, "image")
    assert "--start=10%" in video and "--start=0" in still
    assert "--vo=image" in video and "--hwdec=no" in video
    assert video[-2:] == ["--", str(tmp_path / "a.mp4")]


def test_failures_are_remembered_until_the_file_changes(lib, tmp_path):
    calls = []

    def broken(args):
        calls.append(args)
        raise subprocess.CalledProcessError(2, args, stderr=b"Failed to recognize file format.")

    th = Thumbnails(lib, tmp_path / "thumbs", runner=broken)
    assert th.make("a.mp4") is None and th.failed("a.mp4")
    assert th.make("a.mp4") is None
    assert len(calls) == 1
    future = time.time() + 5
    os.utime(lib.dir / "a.mp4", (future, future))  # the file was replaced
    assert not th.failed("a.mp4")
    th.make("a.mp4")
    assert len(calls) == 2
    assert list((tmp_path / "thumbs").glob(".make-*")) == []  # temp dirs cleaned up


def test_background_queue(lib, tmp_path, fake_thumbnailer):
    th = Thumbnails(lib, tmp_path / "thumbs")
    th.request(["a.mp4", "b.png", "s.mp3", "a.mp4"])
    th.wait()
    assert th.ready("a.mp4") and th.ready("b.png")
    assert sorted(os.path.basename(p) for p in fake_thumbnailer) == ["a.mp4", "b.png"]


def test_clean_removes_orphans(lib, tmp_path, fake_thumbnailer):
    th = Thumbnails(lib, tmp_path / "thumbs")
    th.make("a.mp4")
    (tmp_path / "thumbs" / "gone.mp4.jpg").write_bytes(JPEG)
    (tmp_path / "thumbs" / ".make-x").mkdir()
    th.clean()
    assert sorted(p.name for p in (tmp_path / "thumbs").iterdir()) == ["a.mp4.jpg"]
    th.remove("a.mp4")
    assert not th.path("a.mp4").exists()


@pytest.mark.skipif(not (shutil.which("mpv") and shutil.which("ffmpeg")),
                    reason="needs mpv and ffmpeg")
@pytest.mark.parametrize("name", ["v.mp4", "a.gif", "p.png"])
def test_real_mpv_thumbnail(tmp_path, name):
    lib = MediaLibrary(tmp_path / "media")
    lib.ensure_dirs()
    src = "testsrc2=size=1280x720:rate=10:duration=2"
    extra = ["-frames:v", "1"] if name.endswith(".png") else []
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", src, *extra,
                    str(lib.dir / name)], check=True)
    th = Thumbnails(lib, tmp_path / "thumbs",
                    runner=lambda args: subprocess.run(args, check=True, timeout=60,
                                                       capture_output=True))
    path = th.make(name)
    assert path is not None
    data = path.read_bytes()
    assert data[:3] == b"\xff\xd8\xff"   # a JPEG
    assert 2_000 < len(data) < 200_000   # small, but a real picture
