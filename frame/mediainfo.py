"""Is an artwork file too heavy for a Raspberry Pi 4?

Reads just the container headers (MP4/MOV, Matroska/WebM, GIF) in Python: no
ffprobe on the Pi, and starting an extra mpv costs RAM the Pi doesn't have.
Unknown or unreadable files get no warning; this only ever advises.

The Pi 4 decodes H.264 in hardware up to 1080p (level 4.2) and 8-bit HEVC.
Anything else is decoded in software, with big buffers: that can freeze the
picture or run the 1 GB Pi out of memory (see README, "Preparing artwork").
"""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import BinaryIO

MAX_PIXELS = 1920 * 1088
GIF_MAX_BYTES = 50 * 1024 * 1024
# H.264 profiles beyond 8-bit 4:2:0 (High 10, High 4:2:2, High 4:4:4).
H264_HEAVY_PROFILES = {110: "10-bit", 122: "4:2:2", 244: "4:4:4"}
MAX_BOXES = 10_000


@dataclass
class VideoInfo:
    codec: str | None = None  # "h264", "hevc", "av1", "vp9", "gif", ...
    width: int | None = None
    height: int | None = None
    bit_depth: int | None = None
    h264_profile: int | None = None
    h264_level: int | None = None  # level_idc: 42 means 4.2


# --- MP4 / MOV -------------------------------------------------------------------

MP4_CODECS = {b"avc1": "h264", b"avc3": "h264", b"hvc1": "hevc", b"hev1": "hevc",
              b"av01": "av1", b"vp09": "vp9", b"vp08": "vp8", b"mp4v": "mpeg4"}
MP4_CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl"}


def _boxes(f: BinaryIO, start: int, end: int):
    pos, n = start, 0
    while pos + 8 <= end and n < MAX_BOXES:
        f.seek(pos)
        head = f.read(16)
        if len(head) < 8:
            return
        size, kind = struct.unpack(">I4s", head[:8])
        body = pos + 8
        if size == 1 and len(head) == 16:
            size, body = struct.unpack(">Q", head[8:16])[0], pos + 16
        elif size == 0:
            size = end - pos
        if size < body - pos:
            return
        yield kind, body, min(pos + size, end)
        pos += size
        n += 1


def _avcc(data: bytes, info: VideoInfo) -> None:
    if len(data) >= 4:
        info.h264_profile, info.h264_level = data[1], data[3]
        info.bit_depth = 10 if data[1] == 110 else info.bit_depth or 8


def _hvcc(data: bytes, info: VideoInfo) -> None:
    if len(data) >= 18:
        info.bit_depth = (data[17] & 0x07) + 8


def _mp4_video(f: BinaryIO, start: int, end: int, info: VideoInfo) -> bool:
    """Walk moov/trak/... for the first video track. True once found."""
    for kind, body, stop in _boxes(f, start, end):
        if kind == b"trak":
            handler = None
            for k, b, s in _walk(f, body, stop):
                if k == b"hdlr" and handler is None:
                    f.seek(b + 8)
                    handler = f.read(4)
                elif k == b"stsd" and handler == b"vide":
                    _mp4_stsd(f, b, s, info)
                    return True
        elif kind in MP4_CONTAINERS and _mp4_video(f, body, stop, info):
            return True
    return False


def _walk(f: BinaryIO, start: int, end: int):
    for kind, body, stop in _boxes(f, start, end):
        yield kind, body, stop
        if kind in MP4_CONTAINERS:
            yield from _walk(f, body, stop)


def _mp4_stsd(f: BinaryIO, body: int, stop: int, info: VideoInfo) -> None:
    # stsd: version/flags, entry count, then the first sample entry.
    entry = body + 8
    f.seek(entry)
    head = f.read(36)
    if len(head) < 36:
        return
    size, fourcc = struct.unpack(">I4s", head[:8])
    info.codec = MP4_CODECS.get(fourcc, fourcc.decode("latin-1").strip())
    info.width, info.height = struct.unpack(">HH", head[32:36])
    # Codec configuration boxes follow the 86-byte visual sample entry.
    for kind, b, s in _boxes(f, entry + 86, min(entry + size, stop)):
        f.seek(b)
        data = f.read(min(64, s - b))
        if kind == b"avcC":
            _avcc(data, info)
        elif kind == b"hvcC":
            _hvcc(data, info)


# --- Matroska / WebM -------------------------------------------------------------

MKV_CODECS = {"V_MPEG4/ISO/AVC": "h264", "V_MPEGH/ISO/HEVC": "hevc", "V_AV1": "av1",
              "V_VP9": "vp9", "V_VP8": "vp8"}
SEGMENT, TRACKS, TRACK_ENTRY, CLUSTER = 0x18538067, 0x1654AE6B, 0xAE, 0x1F43B675
TRACK_TYPE, CODEC_ID, CODEC_PRIVATE, VIDEO = 0x83, 0x86, 0x63A2, 0xE0
PIXEL_WIDTH, PIXEL_HEIGHT, COLOUR, BITS_PER_CHANNEL = 0xB0, 0xBA, 0x55B0, 0x55B2


def _vint(f: BinaryIO, keep_marker: bool) -> tuple[int | None, int]:
    """(value, length) of an EBML variable-length integer; value None if unknown."""
    first = f.read(1)
    if not first:
        raise EOFError
    b = first[0]
    length = 1
    while length <= 8 and not b & (0x80 >> (length - 1)):
        length += 1
    if length > 8:
        raise ValueError("bad EBML integer")
    value = b if keep_marker else b & (0xFF >> length)
    rest = f.read(length - 1)
    if len(rest) < length - 1:
        raise EOFError
    for byte in rest:
        value = (value << 8) | byte
    if not keep_marker and value == (1 << (7 * length)) - 1:
        return None, length  # "unknown size"
    return value, length


def _elements(f: BinaryIO, start: int, end: int):
    pos, n = start, 0
    while pos < end and n < MAX_BOXES:
        f.seek(pos)
        try:
            eid, ilen = _vint(f, keep_marker=True)
            size, slen = _vint(f, keep_marker=False)
        except (EOFError, ValueError):
            return
        body = pos + ilen + slen
        stop = end if size is None else min(body + size, end)
        yield eid, body, stop
        if size is None and eid != SEGMENT:
            return
        pos = stop
        n += 1


def _uint(f: BinaryIO, body: int, stop: int) -> int:
    f.seek(body)
    return int.from_bytes(f.read(min(8, stop - body)), "big")


def _mkv_video(f: BinaryIO, end: int, info: VideoInfo) -> None:
    for eid, body, stop in _elements(f, 0, end):
        if eid != SEGMENT:
            continue
        for sid, sbody, sstop in _elements(f, body, stop):
            if sid == CLUSTER:
                return  # media data starts; the track list comes before it
            if sid != TRACKS:
                continue
            for tid, tbody, tstop in _elements(f, sbody, sstop):
                if tid == TRACK_ENTRY and _mkv_track(f, tbody, tstop, info):
                    return
        return


def _mkv_track(f: BinaryIO, start: int, end: int, info: VideoInfo) -> bool:
    track = VideoInfo()
    is_video = False
    private = b""
    for eid, body, stop in _elements(f, start, end):
        if eid == TRACK_TYPE:
            is_video = _uint(f, body, stop) == 1
        elif eid == CODEC_ID:
            f.seek(body)
            codec = f.read(min(64, stop - body)).rstrip(b"\0").decode("latin-1")
            track.codec = MKV_CODECS.get(codec, codec)
        elif eid == CODEC_PRIVATE:
            f.seek(body)
            private = f.read(min(64, stop - body))
        elif eid == VIDEO:
            for vid, vbody, vstop in _elements(f, body, stop):
                if vid == PIXEL_WIDTH:
                    track.width = _uint(f, vbody, vstop)
                elif vid == PIXEL_HEIGHT:
                    track.height = _uint(f, vbody, vstop)
                elif vid == COLOUR:
                    for cid, cbody, cstop in _elements(f, vbody, vstop):
                        if cid == BITS_PER_CHANNEL:
                            track.bit_depth = _uint(f, cbody, cstop) or None
    if not is_video:
        return False
    if track.codec == "h264":
        _avcc(private, track)
    elif track.codec == "hevc":
        _hvcc(private, track)
    info.__dict__.update({k: v for k, v in track.__dict__.items() if v is not None})
    return True


# --- entry points -----------------------------------------------------------------


def probe(path: Path) -> VideoInfo | None:
    """What the headers say about the video in ``path``, or None if unknown."""
    info = VideoInfo()
    try:
        with open(path, "rb") as f:
            head = f.read(16)
            end = os.fstat(f.fileno()).st_size
            if head[:4] == b"GIF8" and len(head) >= 10:
                info.codec = "gif"
                info.width, info.height = struct.unpack("<HH", head[6:10])
            elif head[4:8] in (b"ftyp", b"moov", b"mdat", b"wide", b"free"):
                _mp4_video(f, 0, end, info)
            elif head[:4] == b"\x1a\x45\xdf\xa3":
                _mkv_video(f, end, info)
    except (OSError, ValueError, struct.error):
        return None
    return info if info.codec else None


def heavy_reasons(info: VideoInfo, size: int = 0) -> list[str]:
    """Why a Pi 4 may struggle with this video (empty if it should be fine)."""
    reasons = []
    if info.width and info.height and info.width * info.height > MAX_PIXELS:
        reasons.append(f"{info.width}×{info.height} is larger than 1920×1080")
    if info.codec == "h264":
        if info.h264_profile in H264_HEAVY_PROFILES:
            reasons.append(f"{H264_HEAVY_PROFILES[info.h264_profile]} H.264")
        if info.h264_level and info.h264_level > 42:
            level = info.h264_level
            reasons.append(f"H.264 level {level // 10}.{level % 10} (above 4.2)")
    elif info.codec == "hevc":
        if info.bit_depth and info.bit_depth > 8:
            reasons.append(f"{info.bit_depth}-bit HEVC")
    elif info.codec in ("av1", "vp9"):
        reasons.append(f"{info.codec.upper()} has no hardware decoding on a Pi 4")
    if info.codec == "gif" and size > GIF_MAX_BYTES:
        reasons.append(f"a {size // (1024 * 1024)} MB GIF")
    return reasons


def pi_warning(path: Path) -> str | None:
    """A one-line warning for the UI if ``path`` is likely too heavy for a Pi 4."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return _cached_warning(str(path), st.st_size, st.st_mtime_ns)


@lru_cache(maxsize=512)
def _cached_warning(path: str, size: int, _mtime_ns: int) -> str | None:
    info = probe(Path(path))
    reasons = heavy_reasons(info, size) if info else []
    if not reasons:
        return None
    return ("May be too heavy for a Raspberry Pi 4 (" + ", ".join(reasons) + "): it can "
            "stutter or freeze the frame. Convert it to 1080p H.264 (see README, "
            "Preparing artwork).")
