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
