import json

import pytest

from frame.state import DEFAULTS, StateError, StateStore, validate


def test_missing_file_gives_defaults(tmp_path):
    assert StateStore(tmp_path / "state.json").load() == DEFAULTS


def test_roundtrip_persists_across_instances(tmp_path):
    path = tmp_path / "sub" / "state.json"
    StateStore(path).update(playlist=["a.mp4", "b.gif"], volume=42, muted=True, rotation=90,
                            fit="fill")
    state = StateStore(path).load()  # a fresh instance, as after a reboot
    assert state["playlist"] == ["a.mp4", "b.gif"]
    assert state["volume"] == 42
    assert state["muted"] is True
    assert state["rotation"] == 90
    assert state["fit"] == "fill"
    assert not (tmp_path / "sub" / "state.json.tmp").exists()


def test_corrupt_file_falls_back_and_is_quarantined(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not json")
    assert StateStore(path).load() == DEFAULTS
    assert (tmp_path / "state.json.corrupt").exists()


def test_bad_values_in_file_are_ignored_individually(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"volume": "loud", "rotation": 45, "playlist": ["x.mp4"],
                                "zzz": 1}))
    state = StateStore(path).load()
    assert state["volume"] == DEFAULTS["volume"]
    assert state["rotation"] == 0
    assert state["playlist"] == ["x.mp4"]
    assert "zzz" not in state


def test_volume_is_clamped_and_rounded():
    assert validate({"volume": 150})["volume"] == 100
    assert validate({"volume": -3})["volume"] == 0
    assert validate({"volume": 33.6})["volume"] == 34


@pytest.mark.parametrize(
    "changes",
    [
        {"volume": True},
        {"volume": "50"},
        {"muted": "yes"},
        {"rotation": 45},
        {"rotation": True},
        {"fit": "zoom"},
        {"playlist": "a.mp4"},
        {"playlist": [""]},
        {"playlist": [3]},
        {"sounds": None},
        {"interval": -1},
        {"interval": 1.5},
        {"interval": True},
        {"sound_interval": 10**9},
        {"shuffle": 1},
        {"blank": "no"},
        {"fade": 9},
        {"fade": True},
        {"current": "a.mp4"},
        {"hwdec": ""},
        {"nope": 1},
    ],
)
def test_invalid_values_rejected(changes):
    with pytest.raises(StateError):
        validate(changes)


def test_update_rejects_without_writing(tmp_path):
    store = StateStore(tmp_path / "state.json")
    with pytest.raises(StateError):
        store.update(volume="x")
    assert not (tmp_path / "state.json").exists()


def test_playlist_duplicates_dropped_and_fade_is_float():
    out = validate({"playlist": ["a.mp4", "b.gif", "a.mp4"], "fade": 2})
    assert out == {"playlist": ["a.mp4", "b.gif"], "fade": 2.0}


@pytest.mark.parametrize(
    "old, playlist, sounds",
    [
        ({"current": "a.mp4", "soundtrack": "s.mp3"}, ["a.mp4"], ["s.mp3"]),
        ({"current": None, "soundtrack": None}, [], []),
        ({"current": "a.mp4"}, ["a.mp4"], []),
    ],
)
def test_settings_from_older_versions_are_upgraded(tmp_path, old, playlist, sounds):
    path = tmp_path / "state.json"
    path.write_text(json.dumps(dict(old, volume=33)))
    state = StateStore(path).load()
    assert (state["playlist"], state["sounds"], state["volume"]) == (playlist, sounds, 33)
    assert "current" not in state and "soundtrack" not in state
