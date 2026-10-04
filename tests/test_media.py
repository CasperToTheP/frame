import os

import pytest

from frame.media import MediaError, MediaLibrary, kind_of, safe_name, sniff_ok

from .conftest import MKV, MP3, MP4, PNG, SVG, WEBP, WEBP_ANIMATED


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Sunset.MP4", "Sunset.mp4"),
        ("my art work.mp4", "my_art_work.mp4"),
        ("../../etc/passwd.mp4", "passwd.mp4"),
        ("..\\..\\windows\\evil.mkv", "evil.mkv"),
        ("/abs/path/clip.webm", "clip.webm"),
        (".hidden.png", "hidden.png"),
        ("Café–Noir.gif", "CafeNoir.gif"),
        ("-rf.mp4", "rf.mp4"),
        ("$$$.mp4", "artwork.mp4"),
        ("a" * 300 + ".mp4", "a" * 116 + ".mp4"),
    ],
)
def test_safe_name(raw, expected):
    assert safe_name(raw) == expected


@pytest.mark.parametrize("raw", ["", "script.sh", "noext", "movie.mp4.exe", "../..", ".mp4x"])
def test_safe_name_rejects(raw):
    with pytest.raises(MediaError):
        safe_name(raw)


@pytest.fixture
def lib(tmp_path):
    lib = MediaLibrary(tmp_path / "media")
    lib.ensure_dirs()
    return lib


def upload(lib, name, data):
    f = lib.new_incoming_file()
    f.write(data)
    f.close()
    return lib.add(f.name, name)


def test_add_and_list(lib):
    assert upload(lib, "b.mp4", MP4) == "b.mp4"
    assert upload(lib, "a.mkv", MKV) == "a.mkv"
    assert upload(lib, "c.png", PNG) == "c.png"
    (lib.dir / "notes.txt").write_text("ignored")
    (lib.dir / ".hidden.mp4").write_bytes(MP4)
    items = lib.list()
    assert [i.name for i in items] == ["a.mkv", "b.mp4", "c.png"]
    assert [i.kind for i in items] == ["video", "video", "image"]
    assert items[1].size == len(MP4)
    assert list(lib.incoming.iterdir()) == []


def test_duplicate_names_get_suffix(lib):
    assert upload(lib, "art.mp4", MP4) == "art.mp4"
    assert upload(lib, "art.mp4", MP4) == "art-1.mp4"
    assert upload(lib, "art.mp4", MP4) == "art-2.mp4"


def test_rejects_content_mismatch_and_cleans_up(lib):
    f = lib.new_incoming_file()
    f.write(b"this is not a video at all")
    f.close()
    with pytest.raises(MediaError) as e:
        lib.add(f.name, "fake.mp4")
    assert e.value.status == 415
    assert not os.path.exists(f.name)
    assert lib.list() == []


def test_rejects_empty_file(lib):
    f = lib.new_incoming_file()
    f.close()
    with pytest.raises(MediaError, match="empty"):
        lib.add(f.name, "empty.mp4")


def test_rejects_bad_extension_and_cleans_up(lib):
    f = lib.new_incoming_file()
    f.write(MP4)
    f.close()
    with pytest.raises(MediaError):
        lib.add(f.name, "x.exe")
    assert not os.path.exists(f.name)


@pytest.mark.parametrize("name", ["../state.json", "..", ".", "", "a/b.mp4", "a\\b.mp4",
                                  ".incoming", "x\x00.mp4"])
def test_resolve_rejects_traversal(lib, name):
    with pytest.raises(MediaError):
        lib.resolve(name)


def test_resolve_missing_is_404(lib):
    with pytest.raises(MediaError) as e:
        lib.resolve("nope.mp4")
    assert e.value.status == 404


def test_delete(lib):
    upload(lib, "a.mp4", MP4)
    lib.delete("a.mp4")
    assert lib.list() == []
    with pytest.raises(MediaError):
        lib.delete("a.mp4")


def test_clean_incoming(lib):
    f = lib.new_incoming_file()
    f.write(b"partial")
    f.close()
    lib.clean_incoming()
    assert list(lib.incoming.iterdir()) == []


@pytest.mark.parametrize(
    "name, head",
    [
        ("a.webp", WEBP),
        ("a.bmp", b"BM" + b"\x00" * 20),
        ("a.tiff", b"II*\x00" + b"\x00" * 20),
        ("a.tif", b"MM\x00*" + b"\x00" * 20),
        ("a.svg", SVG),
        ("a.svg", b"<svg xmlns='http://www.w3.org/2000/svg'/>"),
        ("a.mp3", MP3),
        ("a.mp3", b"\xff\xfb\x90\x64" + b"\x00" * 20),
        ("a.m4a", MP4),
        ("a.aac", b"\xff\xf1\x50\x80" + b"\x00" * 20),
        ("a.wav", b"RIFF\x00\x00\x00\x00WAVEfmt " + b"\x00" * 20),
        ("a.flac", b"fLaC" + b"\x00" * 20),
        ("a.ogg", b"OggS" + b"\x00" * 20),
        ("a.opus", b"OggS" + b"\x00" * 20),
    ],
)
def test_sniff_accepts_new_formats(tmp_path, name, head):
    path = tmp_path / name
    path.write_bytes(head)
    assert sniff_ok(path, os.path.splitext(name)[1])


@pytest.mark.parametrize("name", ["a.webp", "a.svg", "a.mp3", "a.wav", "a.flac", "a.ogg"])
def test_sniff_rejects_text_as_new_formats(tmp_path, name):
    path = tmp_path / name
    path.write_bytes(b"just some text, not media")
    assert not sniff_ok(path, os.path.splitext(name)[1])


def test_kinds():
    assert kind_of("x.MP3") == "audio"
    assert kind_of("x.webp") == "image"
    assert kind_of("x.gif") == "animation"
    assert kind_of("x.txt") is None


def test_still_webp_accepted_animated_rejected(lib):
    assert upload(lib, "still.webp", WEBP) == "still.webp"
    f = lib.new_incoming_file()
    f.write(WEBP_ANIMATED)
    f.close()
    with pytest.raises(MediaError, match="animated WebP") as e:
        lib.add(f.name, "anim.webp")
    assert e.value.status == 415
    assert not os.path.exists(f.name)


def test_svg_without_converter_is_a_clear_error(lib, monkeypatch):
    monkeypatch.setattr("frame.media.shutil.which", lambda name: None)
    f = lib.new_incoming_file()
    f.write(SVG)
    f.close()
    with pytest.raises(MediaError, match="librsvg2-bin"):
        lib.add(f.name, "logo.svg")
    assert list(lib.incoming.iterdir()) == []


def test_playable_filters_kind_and_missing(lib):
    upload(lib, "a.mp4", MP4)
    upload(lib, "song.mp3", MP3)
    names = ["song.mp3", "gone.mp4", "a.mp4"]
    assert lib.playable(names, ("video", "image")) == ["a.mp4"]
    assert lib.playable(names, ("audio",)) == ["song.mp3"]
