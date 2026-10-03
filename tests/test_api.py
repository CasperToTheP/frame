import io
import json
import shutil

import pytest

from frame.controller import Controller
from frame.web import create_app

from .conftest import MKV, MP3, MP4, PNG, SVG, WEBP_ANIMATED


@pytest.fixture
def ctl(cfg, ipc, audio_ipc):
    return Controller(cfg, ipc=ipc, audio_ipc=audio_ipc)


@pytest.fixture
def client(cfg, ctl):
    app = create_app(cfg, controller=ctl)
    app.testing = True
    return app.test_client()


def post_file(client, name, data, **form):
    return client.post(
        "/api/media",
        data={"file": (io.BytesIO(data), name), **form},
        content_type="multipart/form-data",
    )


def test_index_renders(client):
    res = client.get("/")
    assert res.status_code == 200
    assert b"Now playing" in res.data


def test_status(client, ipc):
    data = client.get("/api/status").get_json()
    assert data["playback"] == "playing"
    assert data["volume"] == 70
    assert data["player"]["reachable"] is True
    assert data["player"]["hwdec_current"] == "drm"


def test_status_when_player_down(client, ipc):
    ipc.available = False
    data = client.get("/api/status").get_json()
    assert data["playback"] == "player offline"
    assert data["player"]["reachable"] is False


def test_upload_list_and_no_autoplay(client, ipc, cfg):
    res = post_file(client, "Sea Waves.mp4", MP4)
    assert res.status_code == 201
    assert res.get_json()["saved"] == ["Sea_Waves.mp4"]
    assert (cfg.media_dir / "Sea_Waves.mp4").read_bytes() == MP4
    assert not any(c[0] == "load" for c in ipc.calls)  # upload doesn't switch artwork
    media = client.get("/api/media").get_json()["media"]
    assert media == [
        {"name": "Sea_Waves.mp4", "kind": "video", "size": len(MP4),
         "mtime": media[0]["mtime"], "current": False, "soundtrack": False}
    ]
    assert list(cfg.incoming_dir.iterdir()) == []


def test_upload_and_play(client, ipc, ctl):
    res = post_file(client, "a.mkv", MKV, play="1")
    assert res.status_code == 201
    assert ("load", "a.mkv") in ipc.calls
    assert ctl.state.load()["current"] == "a.mkv"


def test_upload_duplicate_name(client):
    post_file(client, "a.mp4", MP4)
    assert post_file(client, "a.mp4", MP4).get_json()["saved"] == ["a-1.mp4"]


def test_upload_bad_extension(client, cfg):
    res = post_file(client, "evil.sh", b"#!/bin/sh")
    assert res.status_code == 415
    assert "unsupported" in res.get_json()["error"]
    assert list(cfg.incoming_dir.iterdir()) == []


def test_upload_wrong_content(client, cfg):
    res = post_file(client, "fake.mp4", b"hello this is text")
    assert res.status_code == 415
    assert list(cfg.incoming_dir.iterdir()) == []
    assert list(p for p in cfg.media_dir.iterdir() if p.is_file()) == []


def test_upload_traversal_name_is_contained(client, cfg):
    res = post_file(client, "../../state.mp4", MP4)
    assert res.get_json()["saved"] == ["state.mp4"]
    assert (cfg.media_dir / "state.mp4").exists()


def test_upload_too_large(client, cfg):
    res = post_file(client, "big.mp4", MP4 + b"\0" * (cfg.max_upload_mb * 1024 * 1024))
    assert res.status_code == 413
    assert "too large" in res.get_json()["error"]


def test_upload_missing_file(client):
    res = client.post("/api/media", data={}, content_type="multipart/form-data")
    assert res.status_code == 400


def test_play(client, ipc, ctl):
    post_file(client, "a.mp4", MP4)
    res = client.post("/api/play", json={"filename": "a.mp4"})
    assert res.status_code == 200
    assert ("load", "a.mp4") in ipc.calls
    assert ("set", "pause", False) in ipc.calls
    assert ctl.state.load()["current"] == "a.mp4"
    assert client.get("/api/media").get_json()["media"][0]["current"] is True


def test_play_missing_file(client):
    res = client.post("/api/play", json={"filename": "nope.mp4"})
    assert res.status_code == 404


def test_play_traversal(client):
    assert client.post("/api/play", json={"filename": "../state.json"}).status_code == 400


def test_play_requires_json(client):
    res = client.post("/api/play", data={"filename": "a.mp4"})
    assert res.status_code == 400


def test_play_invalid_media_restores_previous(client, ipc, ctl):
    post_file(client, "good.mp4", MP4)
    post_file(client, "bad.mp4", MP4)
    client.post("/api/play", json={"filename": "good.mp4"})
    ipc.invalid.add("bad.mp4")
    res = client.post("/api/play", json={"filename": "bad.mp4"})
    assert res.status_code == 422
    assert ipc.calls[-1] == ("load", "good.mp4")
    assert ctl.state.load()["current"] == "good.mp4"


def test_play_while_player_down_is_saved(client, ipc, ctl):
    post_file(client, "a.mp4", MP4)
    ipc.available = False
    res = client.post("/api/play", json={"filename": "a.mp4"})
    assert res.status_code == 200
    assert "warning" in res.get_json()
    assert ctl.state.load()["current"] == "a.mp4"


def test_pause_resume(client, ipc):
    assert client.post("/api/pause").status_code == 200
    assert ("set", "pause", True) in ipc.calls
    assert client.post("/api/resume").status_code == 200
    assert ipc.calls[-1] == ("set", "pause", False)


def test_pause_when_player_down(client, ipc):
    ipc.available = False
    res = client.post("/api/pause")
    assert res.status_code == 503
    assert "error" in res.get_json()


def test_volume(client, ipc, ctl):
    res = client.post("/api/volume", json={"volume": 120})
    assert res.get_json() == {"volume": 100}
    assert ("set", "volume", 100) in ipc.calls
    assert ctl.state.load()["volume"] == 100
    assert client.post("/api/volume", json={"volume": "x"}).status_code == 400
    assert client.post("/api/volume", json={}).status_code == 400


def test_volume_saved_when_player_down(client, ipc, ctl):
    ipc.available = False
    assert client.post("/api/volume", json={"volume": 10}).status_code == 200
    assert ctl.state.load()["volume"] == 10


def test_mute_toggle_and_set(client, ipc):
    assert client.post("/api/mute", json={}).get_json() == {"muted": True}
    assert client.post("/api/mute").get_json() == {"muted": False}
    assert client.post("/api/mute", json={"muted": True}).get_json() == {"muted": True}
    assert ("set", "mute", True) in ipc.calls
    assert client.post("/api/mute", json={"muted": "yes"}).status_code == 400


def test_settings(client, ipc, ctl):
    res = client.post("/api/settings", json={"rotation": 90, "fit": "fill"})
    assert res.status_code == 200
    assert ("set", "video-rotate", 90) in ipc.calls
    assert ("set", "panscan", 1.0) in ipc.calls
    state = ctl.state.load()
    assert (state["rotation"], state["fit"]) == (90, "fill")
    assert client.post("/api/settings", json={"rotation": 45}).status_code == 400
    assert client.post("/api/settings", json={"current": "x.mp4"}).status_code == 400


def test_audio_device_setting(client, ipc):
    client.post("/api/settings", json={"audio_device": "alsa/hdmi:CARD=vc4hdmi1,DEV=0"})
    assert ("set", "audio-device", "alsa/hdmi:CARD=vc4hdmi1,DEV=0") in ipc.calls
    client.post("/api/settings", json={"audio_device": "auto"})
    assert ipc.calls[-1] == ("set", "audio-device", "auto")  # no Pi HDMI in tests


def test_audio_devices(client):
    devices = client.get("/api/audio-devices").get_json()["devices"]
    assert devices[1]["name"] == "alsa/hdmi:CARD=vc4hdmi0,DEV=0"


def test_delete(client, cfg):
    post_file(client, "a.mp4", MP4)
    assert client.delete("/api/media/a.mp4").status_code == 200
    assert not (cfg.media_dir / "a.mp4").exists()
    assert client.delete("/api/media/a.mp4").status_code == 404


def test_delete_current_needs_force(client, ipc, ctl, cfg):
    post_file(client, "a.mp4", MP4)
    client.post("/api/play", json={"filename": "a.mp4"})
    res = client.delete("/api/media/a.mp4")
    assert res.status_code == 409
    assert (cfg.media_dir / "a.mp4").exists()
    res = client.delete("/api/media/a.mp4?force=1")
    assert res.status_code == 200
    assert ("command", "stop") in ipc.calls
    assert ctl.state.load()["current"] is None


def test_delete_traversal(client, cfg):
    cfg.data_dir.joinpath("state.json").write_text("{}")
    res = client.delete("/api/media/..%2Fstate.json")
    assert res.status_code in (400, 404)
    assert cfg.data_dir.joinpath("state.json").exists()


def test_stop(client, ipc, ctl):
    post_file(client, "a.mp4", MP4)
    client.post("/api/play", json={"filename": "a.mp4"})
    assert client.post("/api/stop").status_code == 200
    assert ctl.state.load()["current"] is None


def test_cross_origin_post_refused(client, ipc):
    res = client.post("/api/pause", headers={"Origin": "http://evil.example"})
    assert res.status_code == 403
    assert not ipc.calls
    ok = client.post("/api/pause", headers={"Origin": "http://localhost"})
    assert ok.status_code == 200


def test_supervisor_status_is_reported(client, cfg):
    cfg.run_dir.mkdir(parents=True)
    cfg.player_status_file.write_text(
        json.dumps({"mpv_running": True, "restarts": 2, "display": "card1-HDMI-A-1",
                    "mpv_started_at": None})
    )
    player = client.get("/api/status").get_json()["player"]
    assert player["restarts"] == 2
    assert player["display"] == "card1-HDMI-A-1"


# --- formats and soundtracks --------------------------------------------------


def test_upload_animated_webp_explains(client, cfg):
    res = post_file(client, "punk.webp", WEBP_ANIMATED)
    assert res.status_code == 415
    assert "animated WebP" in res.get_json()["error"]
    assert list(cfg.incoming_dir.iterdir()) == []


def test_audio_file_cannot_be_played_as_artwork(client):
    post_file(client, "song.mp3", MP3)
    res = client.post("/api/play", json={"filename": "song.mp3"})
    assert res.status_code == 400
    assert "Sound" in res.get_json()["error"]


def test_soundtrack_replaces_artwork_sound(client, ipc, audio_ipc, ctl):
    post_file(client, "a.mp4", MP4)
    post_file(client, "song.mp3", MP3)
    client.post("/api/play", json={"filename": "a.mp4"})
    res = client.post("/api/soundtrack", json={"filename": "song.mp3"})
    assert res.status_code == 200
    # The artwork's own sound goes off before the audio player takes the device.
    assert ("set", "aid", "no") in ipc.calls
    assert ("load", "song.mp3") in audio_ipc.calls
    assert ctl.state.load()["soundtrack"] == "song.mp3"
    media = {m["name"]: m for m in client.get("/api/media").get_json()["media"]}
    assert media["song.mp3"]["soundtrack"] is True and media["song.mp3"]["kind"] == "audio"
    assert client.get("/api/status").get_json()["soundtrack"] == "song.mp3"

    # Back to the artwork's own sound.
    audio_ipc.calls.clear()
    ipc.calls.clear()
    assert client.post("/api/soundtrack", json={"filename": None}).status_code == 200
    assert audio_ipc.calls[0] == ("command", "stop")
    assert ipc.calls[-1] == ("set", "aid", "auto")
    assert ctl.state.load()["soundtrack"] is None


def test_soundtrack_follows_artwork_changes(client, ipc, audio_ipc):
    for name, data in [("a.mp4", MP4), ("b.png", PNG), ("song.mp3", MP3)]:
        post_file(client, name, data)
    client.post("/api/soundtrack", json={"filename": "song.mp3"})
    # Nothing on screen yet: the soundtrack waits for a visual.
    assert not any(c[0] == "load" for c in audio_ipc.calls)
    client.post("/api/play", json={"filename": "b.png"})
    assert ("load", "song.mp3") in audio_ipc.calls
    n = len(audio_ipc.calls)
    client.post("/api/play", json={"filename": "a.mp4"})
    # Switching the visual doesn't restart the soundtrack.
    assert ("load", "song.mp3") not in audio_ipc.calls[n:]
    assert ipc.calls[-1] == ("set", "aid", "no")
    client.post("/api/stop")
    assert ("command", "stop") in audio_ipc.calls[n:]


def test_soundtrack_rejects_non_audio_and_missing(client):
    post_file(client, "a.mp4", MP4)
    assert client.post("/api/soundtrack", json={"filename": "a.mp4"}).status_code == 400
    assert client.post("/api/soundtrack", json={"filename": "x.mp3"}).status_code == 404
    assert client.post("/api/soundtrack", json={"filename": 3}).status_code == 400


def test_invalid_soundtrack_restores_previous(client, audio_ipc, ctl):
    for name, data in [("a.mp4", MP4), ("good.mp3", MP3), ("bad.mp3", MP3)]:
        post_file(client, name, data)
    client.post("/api/play", json={"filename": "a.mp4"})
    client.post("/api/soundtrack", json={"filename": "good.mp3"})
    audio_ipc.invalid.add("bad.mp3")
    res = client.post("/api/soundtrack", json={"filename": "bad.mp3"})
    assert res.status_code == 422
    assert ctl.state.load()["soundtrack"] == "good.mp3"
    assert audio_ipc.calls[-2] == ("load", "good.mp3")


def test_upload_audio_with_play_sets_soundtrack(client, ctl):
    post_file(client, "a.mp4", MP4)
    client.post("/api/play", json={"filename": "a.mp4"})
    res = post_file(client, "song.mp3", MP3, play="1")
    assert res.status_code == 201
    state = ctl.state.load()
    assert (state["current"], state["soundtrack"]) == ("a.mp4", "song.mp3")


def test_volume_mute_pause_reach_both_players(client, ipc, audio_ipc):
    client.post("/api/volume", json={"volume": 30})
    client.post("/api/mute", json={"muted": True})
    client.post("/api/pause")
    for fake in (ipc, audio_ipc):
        assert ("set", "volume", 30) in fake.calls
        assert ("set", "mute", True) in fake.calls
        assert ("set", "pause", True) in fake.calls


def test_audio_player_down_does_not_break_artwork(client, ipc, audio_ipc, ctl):
    post_file(client, "a.mp4", MP4)
    post_file(client, "song.mp3", MP3)
    client.post("/api/soundtrack", json={"filename": "song.mp3"})
    audio_ipc.available = False
    assert client.post("/api/play", json={"filename": "a.mp4"}).status_code == 200
    assert ctl.state.load()["current"] == "a.mp4"
    assert client.post("/api/volume", json={"volume": 20}).status_code == 200


def test_delete_soundtrack_needs_force(client, ipc, ctl, cfg):
    post_file(client, "a.mp4", MP4)
    post_file(client, "song.mp3", MP3)
    client.post("/api/play", json={"filename": "a.mp4"})
    client.post("/api/soundtrack", json={"filename": "song.mp3"})
    assert client.delete("/api/media/song.mp3").status_code == 409
    assert client.delete("/api/media/song.mp3?force=1").status_code == 200
    assert ctl.state.load()["soundtrack"] is None
    assert ipc.calls[-1] == ("set", "aid", "auto")
    assert ctl.state.load()["current"] == "a.mp4"


def test_scaling_setting(client, ipc, ctl):
    assert client.post("/api/settings", json={"scaling": "sharp"}).status_code == 200
    assert ("set", "scale", "nearest") in ipc.calls
    assert ctl.state.load()["scaling"] == "sharp"
    assert client.post("/api/settings", json={"scaling": "blurry"}).status_code == 400


@pytest.mark.skipif(not shutil.which("rsvg-convert"), reason="needs rsvg-convert")
def test_svg_upload_becomes_png(client, cfg):
    res = post_file(client, "logo.svg", SVG)
    assert res.status_code == 201
    assert res.get_json()["saved"] == ["logo.png"]
    assert (cfg.media_dir / "logo.png").read_bytes()[:8] == PNG[:8]
    assert list(cfg.incoming_dir.iterdir()) == []
