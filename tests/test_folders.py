import pytest

from frame.folders import FolderStore, clean_path, is_within
from frame.media import MediaError


def test_clean_path():
    assert clean_path(" Japan / Night ") == "Japan/Night"
    assert clean_path("") == "" and clean_path(None) == ""
    for bad in ("a/" * 6, ".hidden", "x" * 41, 5):
        with pytest.raises(MediaError):
            clean_path(bad)


def test_is_within():
    assert is_within("Japan/Night", "Japan") and is_within("Japan", "Japan")
    assert not is_within("Japanese", "Japan")
    assert is_within("anything", "")


def test_store_survives_a_broken_file(tmp_path):
    path = tmp_path / "library.json"
    path.write_text("{not json")
    assert FolderStore(path).load() == {"folders": [], "files": {}}


def test_parents_are_implied_and_expand_is_recursive(tmp_path):
    store = FolderStore(tmp_path / "library.json")
    store.create("", "A")
    store.create("A", "B")
    store.move(["x.mp4"], "A/B")
    store.move(["y.mp4"], "A")
    names = ["z.mp4", "y.mp4", "x.mp4"]
    assert store.expand(["folder:A"], names) == ["x.mp4", "y.mp4"]
    assert store.expand(["folder:A/B", "z.mp4", "x.mp4"], names) == ["x.mp4", "z.mp4"]
    assert store.expand(["z.mp4"], names) == ["z.mp4"]
    # Moving to the top removes the entry.
    store.move(["x.mp4"], "")
    assert store.folder_of("x.mp4") == ""


def test_moving_a_folder_respects_the_depth_limit(tmp_path):
    store = FolderStore(tmp_path / "library.json")
    deep = ""
    for i in range(5):
        deep = store.create(deep, f"d{i}")
    store.create("", "top")
    with pytest.raises(MediaError):
        store.move_folder("d0", "top")  # d0/d1/d2/d3/d4 would become 6 deep
    assert store.move_folder("d0/d1", "top") == ("d0/d1", "top/d1")
