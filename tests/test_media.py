import os

import pytest

from frame.media import MediaError, MediaLibrary, safe_name

from .conftest import MKV, MP4, PNG


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
