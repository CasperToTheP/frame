"""HDMI display and audio detection via sysfs/procfs (read-only).

Kept separate from the player so it can be tested with a fake /sys tree and so
future display features (e.g. scheduled screen power) have one place to live.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_CONNECTOR_RE = re.compile(r"^(card\d+)-(.+)$")


@dataclass(frozen=True)
class Connector:
    card: str  # e.g. "card1"
    name: str  # e.g. "HDMI-A-1"
    connected: bool

    @property
    def device(self) -> str:
        return f"/dev/dri/{self.card}"


def list_connectors(sys_drm: Path) -> list[Connector]:
    """All DRM connectors the kernel knows about. Empty if sysfs is unavailable."""
    out = []
    try:
        entries = sorted(Path(sys_drm).iterdir())
    except OSError:
        return []
    for entry in entries:
        m = _CONNECTOR_RE.match(entry.name)
        if not m:
            continue
        try:
            status = (entry / "status").read_text().strip()
        except OSError:
            continue
        out.append(Connector(m.group(1), m.group(2), status == "connected"))
    return out


def pick_connector(connectors: list[Connector]) -> Connector | None:
    """The connected display to use, preferring HDMI and the lowest port number."""
    connected = [c for c in connectors if c.connected]
    if not connected:
        return None
    connected.sort(key=lambda c: (not c.name.startswith("HDMI"), c.name, c.card))
    return connected[0]


def hdmi_audio_device(connector: Connector | None, proc_asound: Path) -> str | None:
    """mpv ALSA device for the Pi's HDMI port that has the display, if present.

    On Raspberry Pi OS with the vc4-kms-v3d driver, HDMI-A-1 (the port next to
    the USB-C power input) is ALSA card ``vc4hdmi0`` and HDMI-A-2 is ``vc4hdmi1``.
    The ``hdmi:`` PCM wraps the card in ALSA's IEC958 plugin, which this driver
    requires (it rejects plain ``hw:`` PCM formats).
    """
    if connector is None:
        return None
    m = re.match(r"^HDMI-A-(\d+)$", connector.name)
    if not m:
        return None
    card = f"vc4hdmi{int(m.group(1)) - 1}"
    if not (Path(proc_asound) / card).exists():
        return None
    return f"alsa/hdmi:CARD={card},DEV=0"
