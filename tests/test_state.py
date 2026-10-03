import json

import pytest

from frame.state import DEFAULTS, StateError, StateStore, validate


def test_missing_file_gives_defaults(tmp_path):
    assert StateStore(tmp_path / "state.json").load() == DEFAULTS


def test_roundtrip_persists_across_instances(tmp_path):
    path = tmp_path / "sub" / "state.json"
    StateStore(path).update(current="a.mp4", volume=42, muted=True, rotation=90, fit="fill")
    state = StateStore(path).load()  # a fresh instance, as after a reboot
    assert state["current"] == "a.mp4"
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
    path.write_text(json.dumps({"volume": "loud", "rotation": 45, "current": "x.mp4", "zzz": 1}))
    state = StateStore(path).load()
    assert state["volume"] == DEFAULTS["volume"]
    assert state["rotation"] == 0
    assert state["current"] == "x.mp4"
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
        {"current": ""},
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
