from __future__ import annotations

from pathlib import Path

import pytest

from frame.config import Config
from frame.player import InvalidMedia, PlayerError, PlayerUnavailable

# Smallest byte prefixes that pass the upload magic-number check.
MP4 = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 64
MKV = b"\x1a\x45\xdf\xa3" + b"\x00" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
MP3 = b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\x00" * 64
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 64
# Extended WebP header with the animation flag (0x02) set.
WEBP_ANIMATED = b"RIFF\x00\x00\x00\x00WEBPVP8X\x0a\x00\x00\x00\x02" + b"\x00" * 64
SVG = b'<?xml version="1.0"?>\n<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">' \
      b'<rect width="10" height="10" fill="red"/></svg>\n'


class FakeIpc:
    """Stand-in for MpvIpc that records calls."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.props = {
            "pause": False,
            "idle-active": False,
            "path": None,
            "hwdec-current": "drm",
            "audio-device": "auto",
            "audio-device-list": [{"name": "auto", "description": "Autoselect"},
                                  {"name": "alsa/hdmi:CARD=vc4hdmi0,DEV=0",
                                   "description": "HDMI 0"}],
        }
        self.available = True
        self.invalid: set[str] = set()

    def _check(self):
        if not self.available:
            raise PlayerUnavailable("down")

    def command(self, *args):
        self._check()
        self.calls.append(("command",) + args)
        if args[0] == "stop":
            self.props["path"] = None
            self.props["idle-active"] = True

    def ping(self):
        return self.available

    def get(self, name, default=None):
        self._check()
        return self.props.get(name, default)

    def set(self, name, value):
        self._check()
        if name == "bogus":
            raise PlayerError("property not found")
        self.calls.append(("set", name, value))
        self.props[name] = value

    def load(self, path, wait=6.0):
        self._check()
        self.calls.append(("load", Path(path).name))
        if Path(path).name in self.invalid:
            # Like mpv: the old file is gone and the player is idle.
            self.props["path"] = None
            self.props["idle-active"] = True
            raise InvalidMedia("Failed to recognize file format.")
        self.props["path"] = str(path)
        self.props["idle-active"] = False


# A tiny valid JPEG, written by the stand-in thumbnail maker below.
JPEG = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffdb004300080606070605080707070909080a0c140d0c0b0b0c"
    "1912130f141d1a1f1e1d1a1c1c20242e2720222c231c1c2837292c30313434341f27393d38323c2e333432ffc0000b"
    "080001000101011100ffc4001f0000010501010101010100000000000000000102030405060708090a0bffc400b510"
    "0002010303020403050504040000017d01020300041105122131410613516107227114328191a1082342b1c11552d1"
    "f02433627282090a161718191a25262728292a3435363738393a434445464748494a535455565758595a6364656667"
    "68696a737475767778797a838485868788898a92939495969798999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2"
    "c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9dae1e2e3e4e5e6e7e8e9eaf1f2f3f4f5f6f7f8f9faffda0008010100003f00"
    "fbd3ffd9"
)


@pytest.fixture(autouse=True)
def fake_thumbnailer(monkeypatch):
    """Don't run a real mpv for thumbnails in unit tests; pretend it worked."""
    made = []

    def run(args):
        outdir = next(a.split("=", 1)[1] for a in args if a.startswith("--vo-image-outdir="))
        made.append(args[-1])
        Path(outdir, "00000001.jpg").write_bytes(JPEG)

    monkeypatch.setattr("frame.thumbs.run_mpv", run)
    return made


@pytest.fixture
def cfg(tmp_path) -> Config:
    return Config(
        data_dir=tmp_path / "data",
        run_dir=tmp_path / "run",
        sys_drm=tmp_path / "sys_drm",
        proc_asound=tmp_path / "asound",
        max_upload_mb=5,
        reserve_mb=0,
    )


@pytest.fixture
def ipc() -> FakeIpc:
    return FakeIpc()


@pytest.fixture
def audio_ipc() -> FakeIpc:
    """The audio-only mpv: idle until a soundtrack is chosen."""
    fake = FakeIpc()
    fake.props.update({"idle-active": True, "path": None})
    return fake
