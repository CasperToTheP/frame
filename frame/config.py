"""Static configuration, read from environment variables.

Defaults match the installed layout (see README). Every path can be overridden,
which is how tests and local development run without root or a Raspberry Pi.
Installed systems can set overrides in /etc/default/frame.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    data_dir: Path = Path("/var/lib/frame")
    run_dir: Path = Path("/run/frame")
    host: str = "0.0.0.0"
    port: int = 8080
    max_upload_mb: int = 4096
    # Keep this much disk free after an upload so the SD card never fills up.
    reserve_mb: int = 256
    mpv_bin: str = "mpv"
    sys_drm: Path = Path("/sys/class/drm")
    proc_asound: Path = Path("/proc/asound")
    # The frame's own Wi-Fi network, used when the home Wi-Fi can't be reached
    # (see netwatch.py). The installer generates the password.
    hotspot_ssid: str = "Frame"
    hotspot_password: str = ""

    @property
    def media_dir(self) -> Path:
        return self.data_dir / "media"

    @property
    def library_file(self) -> Path:
        # Folders: which folder each file is filed under (folders.py).
        return self.data_dir / "library.json"

    @property
    def thumb_dir(self) -> Path:
        return self.data_dir / "thumbs"

    @property
    def incoming_dir(self) -> Path:
        # Same filesystem as media_dir so finished uploads can be renamed atomically.
        return self.media_dir / ".incoming"

    @property
    def state_file(self) -> Path:
        return self.data_dir / "state.json"

    @property
    def mpv_socket(self) -> Path:
        return self.run_dir / "mpv.sock"

    @property
    def audio_socket(self) -> Path:
        # The second, audio-only mpv that plays a separate soundtrack.
        return self.run_dir / "audio.sock"

    @property
    def player_status_file(self) -> Path:
        return self.run_dir / "player.json"

    @property
    def network_status_file(self) -> Path:
        return self.run_dir / "network.json"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Config:
        env = os.environ if env is None else env
        d = cls()
        return cls(
            data_dir=Path(env.get("FRAME_DATA_DIR", d.data_dir)),
            run_dir=Path(env.get("FRAME_RUN_DIR", d.run_dir)),
            host=env.get("FRAME_HOST", d.host),
            port=int(env.get("FRAME_PORT", d.port)),
            max_upload_mb=int(env.get("FRAME_MAX_UPLOAD_MB", d.max_upload_mb)),
            reserve_mb=int(env.get("FRAME_RESERVE_MB", d.reserve_mb)),
            mpv_bin=env.get("FRAME_MPV_BIN", d.mpv_bin),
            sys_drm=Path(env.get("FRAME_SYS_DRM", d.sys_drm)),
            proc_asound=Path(env.get("FRAME_PROC_ASOUND", d.proc_asound)),
            hotspot_ssid=env.get("FRAME_HOTSPOT_SSID", d.hotspot_ssid),
            hotspot_password=env.get("FRAME_HOTSPOT_PASSWORD", d.hotspot_password),
        )


def setup_logging() -> None:
    """Log to stderr without timestamps; journald adds its own."""
    level = os.environ.get("FRAME_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s")
