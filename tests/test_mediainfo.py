"""Header-only "too heavy for a Pi 4?" checks, on hand-built minimal files."""

import struct

import pytest

from frame.mediainfo import pi_warning, probe


def box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + kind + payload


def mp4(width, height, codec=b"avc1", level=41, profile=100, bit_depth=8,
        moov_last=False) -> bytes:
    """A skeleton MP4: ftyp, a sound track, then a video track with its codec config."""
    if codec in (b"avc1", b"avc3"):
        config = box(b"avcC", bytes([1, profile, 0, level]) + b"\xff\xe1")
    else:
        hvcc = bytearray(23)
        hvcc[17] = 0xF8 | (bit_depth - 8)
        config = box(b"hvcC", bytes(hvcc))
    entry_body = bytes(24) + struct.pack(">HH", width, height) + bytes(50) + config
    stsd = box(b"stsd", struct.pack(">II", 0, 1) + box(codec, entry_body))

    def trak(handler, sample_desc):
        hdlr = box(b"hdlr", bytes(8) + handler + bytes(12))
        stbl = box(b"stbl", sample_desc)
        return box(b"trak", box(b"mdia", hdlr + box(b"minf", stbl)))

    sound = trak(b"soun", box(b"stsd", struct.pack(">II", 0, 1) + box(b"mp4a", bytes(28))))
    moov = box(b"moov", sound + trak(b"vide", stsd))
    mdat = box(b"mdat", bytes(100))
    ftyp = box(b"ftyp", b"isom" + bytes(4))
    return ftyp + (mdat + moov if moov_last else moov + mdat)


def ebml(eid: int, payload: bytes) -> bytes:
    id_bytes = eid.to_bytes((eid.bit_length() + 7) // 8, "big")
    size = (0x0100000000000000 | len(payload)).to_bytes(8, "big")  # 8-byte EBML size
    return id_bytes + size + payload


def mkv(codec_id: bytes, width, height, bits=None, private=b"") -> bytes:
    video = ebml(0xB0, width.to_bytes(2, "big")) + ebml(0xBA, height.to_bytes(2, "big"))
    if bits:
        video += ebml(0x55B0, ebml(0x55B2, bytes([bits])))
    audio_track = ebml(0xAE, ebml(0x83, b"\x02") + ebml(0x86, b"A_OPUS"))
    video_track = ebml(0xAE, ebml(0x83, b"\x01") + ebml(0x86, codec_id)
                       + (ebml(0x63A2, private) if private else b"") + ebml(0xE0, video))
    tracks = ebml(0x1654AE6B, audio_track + video_track)
    info = ebml(0x1549A966, b"\x00" * 10)
    segment = ebml(0x18538067, info + tracks + ebml(0x1F43B675, bytes(50)))
    return ebml(0x1A45DFA3, ebml(0x4282, b"webm")) + segment


def gif(width, height) -> bytes:
    return b"GIF89a" + struct.pack("<HH", width, height) + bytes(20)


def write(tmp_path, name, data):
    path = tmp_path / name
    path.write_bytes(data)
    return path


def test_mp4_details(tmp_path):
    info = probe(write(tmp_path, "a.mp4", mp4(2880, 1620, level=50)))
    assert (info.codec, info.width, info.height, info.h264_level) == ("h264", 2880, 1620, 50)


def test_mp4_with_index_at_the_end(tmp_path):
    info = probe(write(tmp_path, "a.mp4", mp4(1280, 720, moov_last=True)))
    assert (info.width, info.height) == (1280, 720)


@pytest.mark.parametrize("data, expected", [
    (mp4(1920, 1080, level=41), None),
    (mp4(1080, 1920, level=42), None),  # portrait 1080p
    (mp4(3840, 2160, level=51), "3840×2160 is larger than 1920×1080"),
    (mp4(1920, 1080, level=51), "H.264 level 5.1"),
    (mp4(1920, 1080, profile=110), "10-bit H.264"),
    (mp4(1920, 1080, codec=b"hvc1", bit_depth=10), "10-bit HEVC"),
    (mp4(1920, 1080, codec=b"hvc1", bit_depth=8), None),
])
def test_mp4_warnings(tmp_path, data, expected):
    warning = pi_warning(write(tmp_path, "v.mp4", data))
    if expected is None:
        assert warning is None
    else:
        assert expected in warning and "README" in warning


def test_webm_and_mkv(tmp_path):
    vp9 = write(tmp_path, "a.webm", mkv(b"V_VP9", 1280, 720))
    assert probe(vp9).codec == "vp9"
    assert "VP9" in pi_warning(vp9)
    hevc = write(tmp_path, "b.mkv", mkv(b"V_MPEGH/ISO/HEVC", 3840, 2160, bits=10))
    info = probe(hevc)
    assert (info.codec, info.width, info.height, info.bit_depth) == ("hevc", 3840, 2160, 10)
    avc = write(tmp_path, "c.mkv", mkv(b"V_MPEG4/ISO/AVC", 1920, 1080,
                                       private=bytes([1, 100, 0, 40])))
    assert probe(avc).h264_level == 40 and pi_warning(avc) is None


def test_gif(tmp_path, monkeypatch):
    small = write(tmp_path, "a.gif", gif(800, 600))
    assert probe(small).codec == "gif" and pi_warning(small) is None
    assert "larger than 1920×1080" in pi_warning(write(tmp_path, "b.gif", gif(2500, 2500)))
    monkeypatch.setattr("frame.mediainfo.GIF_MAX_BYTES", 10)
    assert "MB GIF" in pi_warning(write(tmp_path, "c.gif", gif(400, 400)))


@pytest.mark.parametrize("data", [b"", b"\x00\x00\x00\x08ftyp", b"\x00\x00\xff\xffmoov" + bytes(8),
                                  b"\x1a\x45\xdf\xa3\xff", b"GIF8", b"hello world" * 10,
                                  mp4(1920, 1080)[:60]])
def test_garbage_is_never_an_error(tmp_path, data):
    path = write(tmp_path, "x.mp4", data)
    probe(path)
    pi_warning(path)
    assert pi_warning(tmp_path / "missing.mp4") is None
