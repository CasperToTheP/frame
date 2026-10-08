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
         "mtime": media[0]["mtime"], "in_playlist": False, "in_sounds": False,
         "playing": False, "warning": None, "thumb": int(media[0]["mtime"]), "folder": ""}
    ]
    assert list(cfg.incoming_dir.iterdir()) == []



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



def test_play_missing_file(client):
    res = client.post("/api/play", json={"filename": "nope.mp4"})
    assert res.status_code == 404


def test_play_traversal(client):
    assert client.post("/api/play", json={"filename": "../state.json"}).status_code == 400


def test_play_requires_json(client):
    res = client.post("/api/play", data={"filename": "a.mp4"})
    assert res.status_code == 400




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



def test_delete_traversal(client, cfg):
    cfg.data_dir.joinpath("state.json").write_text("{}")
    res = client.delete("/api/media/..%2Fstate.json")
    assert res.status_code in (400, 404)
    assert cfg.data_dir.joinpath("state.json").exists()



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








def test_volume_mute_pause_reach_both_players(client, ipc, audio_ipc):
    client.post("/api/volume", json={"volume": 30})
    client.post("/api/mute", json={"muted": True})
    client.post("/api/pause")
    for fake in (ipc, audio_ipc):
        assert ("set", "volume", 30) in fake.calls
        assert ("set", "mute", True) in fake.calls
        assert ("set", "pause", True) in fake.calls




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



# --- playlists ------------------------------------------------------------------


def state(ctl):
    return ctl.state.load()


def test_play_shows_only_that_artwork(client, ctl):
    post_file(client, "a.mp4", MP4)
    post_file(client, "b.png", PNG)
    client.post("/api/playlist", json={"items": ["a.mp4", "b.png"]})
    res = client.post("/api/play", json={"filename": "b.png"})
    assert res.status_code == 200 and "warning" not in res.get_json()
    assert state(ctl)["playlist"] == ["b.png"]
    # The player picks this up from state.json; the web service doesn't load files itself.
    assert client.get("/api/status").get_json()["current"] == "b.png"


def test_play_missing_or_audio_or_traversal(client):
    post_file(client, "song.mp3", MP3)
    assert client.post("/api/play", json={"filename": "nope.mp4"}).status_code == 404
    assert client.post("/api/play", json={"filename": "song.mp3"}).status_code == 400
    assert client.post("/api/play", json={"filename": "../state.json"}).status_code == 400
    assert client.post("/api/play", data={"filename": "a.mp4"}).status_code == 400


def test_play_while_player_down_is_saved(client, ipc, ctl):
    post_file(client, "a.mp4", MP4)
    ipc.available = False
    res = client.post("/api/play", json={"filename": "a.mp4"})
    assert res.status_code == 200
    assert "warning" in res.get_json()
    assert state(ctl)["playlist"] == ["a.mp4"]


def test_playlist_items_interval_shuffle(client, ctl):
    for name, data in [("a.mp4", MP4), ("b.png", PNG), ("song.mp3", MP3)]:
        post_file(client, name, data)
    res = client.post("/api/playlist", json={"items": ["b.png", "a.mp4"], "interval": 60,
                                             "shuffle": True})
    assert res.status_code == 200
    assert res.get_json()["items"] == ["b.png", "a.mp4"]
    st = state(ctl)
    assert (st["playlist"], st["interval"], st["shuffle"]) == (["b.png", "a.mp4"], 60, True)
    # Audio, missing files and bad values are refused.
    assert client.post("/api/playlist", json={"items": ["song.mp3"]}).status_code == 400
    assert client.post("/api/playlist", json={"items": ["x.mp4"]}).status_code == 404
    assert client.post("/api/playlist", json={"items": "a.mp4"}).status_code == 400
    assert client.post("/api/playlist", json={"interval": -5}).status_code == 400
    assert client.post("/api/playlist", json={"colour": "red"}).status_code == 400
    assert state(ctl)["playlist"] == ["b.png", "a.mp4"]


def test_sounds_list(client, ctl):
    for name, data in [("a.mp4", MP4), ("s1.mp3", MP3), ("s2.mp3", MP3)]:
        post_file(client, name, data)
    res = client.post("/api/sounds", json={"items": ["s2.mp3", "s1.mp3"], "interval": 0,
                                           "shuffle": True})
    assert res.status_code == 200
    st = state(ctl)
    assert (st["sounds"], st["sound_interval"], st["sound_shuffle"]) == \
        (["s2.mp3", "s1.mp3"], 0, True)
    assert client.post("/api/sounds", json={"items": ["a.mp4"]}).status_code == 400
    # One sound / back to the artwork's own sound (the older API).
    assert client.post("/api/soundtrack", json={"filename": "s1.mp3"}).status_code == 200
    assert state(ctl)["sounds"] == ["s1.mp3"]
    assert client.post("/api/soundtrack", json={"filename": None}).status_code == 200
    assert state(ctl)["sounds"] == []
    assert client.post("/api/soundtrack", json={"filename": 3}).status_code == 400


def test_add_puts_files_in_the_right_list(client, ctl):
    for name, data in [("a.mp4", MP4), ("b.png", PNG), ("song.mp3", MP3)]:
        post_file(client, name, data)
    for name in ["a.mp4", "song.mp3", "b.png", "a.mp4"]:
        assert client.post("/api/add", json={"filename": name}).status_code == 200
    st = state(ctl)
    assert (st["playlist"], st["sounds"]) == (["a.mp4", "b.png"], ["song.mp3"])
    assert client.post("/api/add", json={"filename": "nope.gif"}).status_code == 404


def test_upload_with_add(client, ctl):
    post_file(client, "a.mp4", MP4, add="1")
    post_file(client, "song.mp3", MP3, add="1")
    st = state(ctl)
    assert (st["playlist"], st["sounds"]) == (["a.mp4"], ["song.mp3"])


def test_upload_with_play(client, ctl):
    post_file(client, "a.mkv", MKV, play="1")
    assert state(ctl)["playlist"] == ["a.mkv"]
    post_file(client, "song.mp3", MP3, play="1")
    assert state(ctl)["sounds"] == ["song.mp3"]


def test_next_requests_reach_the_player(client, cfg):
    assert client.post("/api/next", json={"which": "visual"}).status_code == 200
    assert (cfg.run_dir / "next-visual").exists()
    assert client.post("/api/next", json={"which": "sound"}).status_code == 200
    assert (cfg.run_dir / "next-sound").exists()
    assert client.post("/api/next", json={"which": "both"}).status_code == 400


def test_black_screen_keeps_the_playlist(client, ctl):
    post_file(client, "a.mp4", MP4, add="1")
    assert client.post("/api/stop").status_code == 200
    st = state(ctl)
    assert st["blank"] is True and st["playlist"] == ["a.mp4"]
    assert client.get("/api/status").get_json()["current"] is None
    assert client.post("/api/start").status_code == 200
    assert state(ctl)["blank"] is False
    assert client.get("/api/status").get_json()["current"] == "a.mp4"


def test_status_reports_what_the_player_plays(client, cfg):
    post_file(client, "a.mp4", MP4, add="1")
    post_file(client, "b.png", PNG, add="1")
    cfg.run_dir.mkdir(parents=True, exist_ok=True)
    reported = {"visual": {"current": "b.png", "position": 2, "count": 2, "next_at": 123.0},
                "sound": {"current": None, "position": None, "count": 0, "next_at": None},
                "paused": False, "error": None}
    cfg.player_status_file.write_text(json.dumps({"mpv_running": True, "playlist": reported}))
    data = client.get("/api/status").get_json()
    assert data["current"] == "b.png"
    assert data["now"]["visual"]["next_at"] == 123.0
    assert "playlist" not in data["player"]
    media = {m["name"]: m for m in client.get("/api/media").get_json()["media"]}
    assert media["b.png"]["playing"] and not media["a.mp4"]["playing"]
    assert media["a.mp4"]["in_playlist"]


def test_delete_listed_file_needs_force(client, ctl, cfg):
    post_file(client, "a.mp4", MP4, add="1")
    post_file(client, "song.mp3", MP3, add="1")
    for name in ["a.mp4", "song.mp3"]:
        res = client.delete(f"/api/media/{name}")
        assert res.status_code == 409
        assert (cfg.media_dir / name).exists()
        assert client.delete(f"/api/media/{name}?force=1").status_code == 200
        assert not (cfg.media_dir / name).exists()
    st = state(ctl)
    assert (st["playlist"], st["sounds"]) == ([], [])


def test_fade_setting(client, ctl):
    assert client.post("/api/settings", json={"fade": 2}).status_code == 200
    assert state(ctl)["fade"] == 2.0
    assert client.post("/api/settings", json={"fade": 30}).status_code == 400


def test_index_renders_playlists(client):
    post_file(client, "a.mp4", MP4, add="1")
    post_file(client, "b.gif", b"GIF89a" + b"\0" * 32, add="1")
    post_file(client, "song.mp3", MP3, add="1")
    client.post("/api/saved", json={"name": "Mix"})
    html = client.get("/").get_data(as_text=True)
    assert 'data-view="edit/Mix"' in html and 'href="#edit/Mix"' in html
    assert "Change every" in html and "Change track" in html


def test_upload_warns_about_heavy_video(client, cfg):
    from .test_mediainfo import mp4

    res = post_file(client, "big.mp4", mp4(2880, 1620, level=50))
    assert res.status_code == 201
    assert "2880×1620" in res.get_json()["warning"]
    media = {m["name"]: m for m in client.get("/api/media").get_json()["media"]}
    assert "H.264 level 5.0" in media["big.mp4"]["warning"]
    assert "May be too heavy" in client.get("/").get_data(as_text=True)
    ok = post_file(client, "ok.mp4", mp4(1920, 1080, level=41))
    assert "warning" not in ok.get_json()


def test_network_and_hotspot_shown(cfg, ipc, audio_ipc):
    import dataclasses

    from frame.netwatch import write_status

    cfg = dataclasses.replace(cfg, hotspot_ssid="Frame", hotspot_password="abc23def45")
    write_status(cfg.network_status_file, "wifi", "Home")
    app = create_app(cfg, controller=Controller(cfg, ipc=ipc, audio_ipc=audio_ipc))
    client = app.test_client()
    net = client.get("/api/status").get_json()["network"]
    assert net == {"mode": "wifi", "network": "Home", "hotspot_ssid": "Frame",
                   "hotspot_password": "abc23def45"}
    page = client.get("/").get_data(as_text=True)
    assert "Wi-Fi “Home”" in page and "abc23def45" in page and "10.42.0.1" in page


def test_auto_hwdec_is_translated_for_the_running_mpv(client, ipc):
    ipc.props["mpv-version"] = "mpv v0.40.0"
    res = client.post("/api/settings", json={"hwdec": "auto"})
    assert res.get_json()["hwdec"] == "auto"
    assert ("set", "hwdec", "v4l2m2m-copy,auto-safe") in ipc.calls


# --- saved playlists and previews ---------------------------------------------


def test_save_load_and_delete_named_playlists(client, ctl):
    for name, data in [("a.mp4", MP4), ("b.png", PNG), ("s.mp3", MP3)]:
        post_file(client, name, data)
    client.post("/api/playlist", json={"items": ["a.mp4", "b.png"], "interval": 60})
    client.post("/api/sounds", json={"items": ["s.mp3"]})
    client.post("/api/settings", json={"fade": 2})
    assert client.post("/api/saved", json={"name": "  Evening  "}).get_json()["name"] == "Evening"

    client.post("/api/playlist", json={"items": ["b.png"], "interval": 600})
    client.post("/api/sounds", json={"items": []})
    client.post("/api/saved", json={"name": "Sleeping"})
    saved = {p["name"]: p for p in client.get("/api/status").get_json()["saved"]}
    assert saved["Sleeping"]["active"] and not saved["Evening"]["active"]
    assert (saved["Evening"]["count"], saved["Evening"]["sound_count"]) == (2, 1)
    assert saved["Evening"]["first"] == "a.mp4"

    client.post("/api/stop")
    res = client.post("/api/saved/load", json={"name": "Evening"})
    assert res.status_code == 200
    st = state(ctl)
    assert (st["playlist"], st["interval"], st["sounds"], st["fade"], st["blank"]) == \
        (["a.mp4", "b.png"], 60, ["s.mp3"], 2.0, False)
    saved = {p["name"]: p for p in client.get("/api/status").get_json()["saved"]}
    assert saved["Evening"]["active"] and not saved["Sleeping"]["active"]

    assert client.delete("/api/saved/Sleeping").status_code == 200
    assert list(state(ctl)["saved"]) == ["Evening"]
    assert client.delete("/api/saved/Sleeping").status_code == 404
    assert client.post("/api/saved/load", json={"name": "Nope"}).status_code == 404


def test_save_playlist_validation(client):
    assert client.post("/api/saved", json={"name": "Empty"}).status_code == 400  # no artwork
    post_file(client, "a.mp4", MP4, add="1")
    assert client.post("/api/saved", json={"name": ""}).status_code == 400
    assert client.post("/api/saved", json={"name": "x" * 41}).status_code == 400
    assert client.post("/api/saved", json={"name": 5}).status_code == 400
    assert client.post("/api/saved", json={"name": "Ok"}).status_code == 200


def test_deleting_a_file_removes_it_from_saved_playlists(client, ctl):
    post_file(client, "a.mp4", MP4, add="1")
    post_file(client, "b.png", PNG, add="1")
    client.post("/api/saved", json={"name": "Both"})
    client.post("/api/playlist", json={"items": ["a.mp4"]})
    assert client.delete("/api/media/b.png").status_code == 200
    assert state(ctl)["saved"]["Both"]["playlist"] == ["a.mp4"]


def test_media_previews(client, cfg):
    post_file(client, "a.mp4", MP4)
    res = client.get("/media/a.mp4", headers={"Range": "bytes=0-9"})
    assert res.status_code == 206 and res.data == MP4[:10]
    assert res.headers["Content-Type"] == "video/mp4"
    assert client.get("/media/nope.mp4").status_code == 404
    cfg.data_dir.joinpath("state.json").write_text("{}")
    assert client.get("/media/..%2Fstate.json").status_code in (400, 404)
    assert client.get("/media/.incoming").status_code in (400, 404)


def test_page_has_tabs_saved_chips_and_previews(client):
    post_file(client, "a.mp4", MP4, add="1")
    post_file(client, "b.png", PNG, add="1")
    client.post("/api/saved", json={"name": "Sleeping"})
    html = client.get("/").get_data(as_text=True)
    for view in ("now", "playlists", "library", "settings"):
        assert f'data-view="{view}"' in html and f'href="#{view}"' in html
    assert 'data-load="Sleeping"' in html
    assert 'src="/thumb/a.mp4?v=' in html and 'src="/thumb/b.png?v=' in html
    assert 'data-jump="a.mp4"' in html and 'id="mini"' in html


def test_thumbnails(client, ctl, fake_thumbnailer):
    from .conftest import JPEG

    post_file(client, "a.mp4", MP4)
    post_file(client, "s.mp3", MP3)
    ctl.thumbs.wait()
    res = client.get("/thumb/a.mp4?v=1")
    assert res.status_code == 200 and res.data == JPEG
    assert res.headers["Content-Type"] == "image/jpeg"
    assert "max-age" in res.headers["Cache-Control"]
    assert client.get("/thumb/s.mp3").status_code == 404      # music has none
    assert client.get("/thumb/nope.mp4").status_code == 404
    assert client.get("/thumb/..%2Fstate.json").status_code in (400, 404)
    # Deleting the file deletes its thumbnail.
    client.delete("/api/media/a.mp4")
    assert not ctl.thumbs.path("a.mp4").exists()


def test_jump_to_an_item(client, cfg):
    post_file(client, "a.mp4", MP4, add="1")
    post_file(client, "b.png", PNG, add="1")
    def jump(name):
        return client.post("/api/next", json={"which": "visual", "filename": name})

    assert jump("b.png").status_code == 200
    assert (cfg.run_dir / "next-visual").read_text() == "b.png"
    assert jump("x.png").status_code == 400



# --- editing playlists without touching what plays ------------------------------


def live(ctl):
    st = state(ctl)
    return st["playlist"], st["sounds"], st["interval"]


def test_new_playlist_is_built_without_changing_the_frame(client, ctl):
    for name, data in [("a.mp4", MP4), ("b.png", PNG), ("s.mp3", MP3)]:
        post_file(client, name, data)
    client.post("/api/play", json={"filename": "a.mp4"})
    before = live(ctl)

    assert client.post("/api/saved/new", json={"name": "Sleeping"}).status_code == 200
    assert client.post("/api/saved/new", json={"name": "Sleeping"}).status_code == 409
    def add(filename):
        return client.post("/api/saved/add", json={"name": "Sleeping", "filename": filename})

    assert add("b.png").get_json()["added"]
    assert add("s.mp3").status_code == 200
    res = client.post("/api/saved/edit", json={"name": "Sleeping", "interval": 900, "fade": 3})
    assert res.get_json() == {"ok": True, "name": "Sleeping", "playing": False}
    assert live(ctl) == before  # the frame still shows a.mp4

    saved = state(ctl)["saved"]["Sleeping"]
    assert (saved["playlist"], saved["sounds"], saved["interval"], saved["fade"]) == \
        (["b.png"], ["s.mp3"], 900, 3.0)
    summary = client.get("/api/status").get_json()["saved"]
    entry = next(p for p in summary if p["name"] == "Sleeping")
    assert not entry["playing"] and entry["items"] == ["b.png"]

    # Playing it puts it on the frame; editing it then changes the frame too.
    client.post("/api/saved/load", json={"name": "Sleeping"})
    assert live(ctl) == (["b.png"], ["s.mp3"], 900)
    res = client.post("/api/saved/edit", json={"name": "Sleeping", "items": ["a.mp4", "b.png"]})
    assert res.get_json()["playing"] is True
    assert live(ctl)[0] == ["a.mp4", "b.png"]


def test_play_now_detaches_from_the_playlist(client, ctl):
    post_file(client, "a.mp4", MP4)
    post_file(client, "b.png", PNG)
    client.post("/api/saved/new", json={"name": "Mix"})
    client.post("/api/saved/edit", json={"name": "Mix", "items": ["a.mp4", "b.png"]})
    client.post("/api/saved/load", json={"name": "Mix"})
    client.post("/api/play", json={"filename": "b.png"})
    st = state(ctl)
    assert st["active"] is None and st["saved"]["Mix"]["playlist"] == ["a.mp4", "b.png"]
    # Editing it now doesn't touch the frame.
    client.post("/api/saved/edit", json={"name": "Mix", "items": ["a.mp4"]})
    assert state(ctl)["playlist"] == ["b.png"]


def test_rename_and_delete_keep_playback(client, ctl):
    post_file(client, "a.mp4", MP4, add="1")
    client.post("/api/saved", json={"name": "Old"})
    assert state(ctl)["active"] == "Old"
    res = client.post("/api/saved/edit", json={"name": "Old", "rename": "New"})
    assert res.get_json()["name"] == "New"
    st = state(ctl)
    assert list(st["saved"]) == ["New"] and st["active"] == "New"
    client.post("/api/saved/new", json={"name": "Other"})
    res = client.post("/api/saved/edit", json={"name": "New", "rename": "Other"})
    assert res.status_code == 409
    client.delete("/api/saved/New")
    st = state(ctl)
    assert st["active"] is None and st["playlist"] == ["a.mp4"]  # still playing


def test_edit_validation(client):
    post_file(client, "a.mp4", MP4)
    post_file(client, "s.mp3", MP3)
    client.post("/api/saved/new", json={"name": "P"})
    bad = [{"items": ["s.mp3"]}, {"sounds": ["a.mp4"]}, {"items": ["gone.mp4"]},
           {"items": "a.mp4"}, {"interval": -1}, {"colour": 1}]
    for changes in bad:
        res = client.post("/api/saved/edit", json={"name": "P", **changes})
        assert res.status_code in (400, 404), changes
    assert client.post("/api/saved/edit", json={"name": "Nope", "fade": 1}).status_code == 404
    # An empty playlist can't be played.
    assert client.post("/api/saved/load", json={"name": "P"}).status_code == 400


def test_upload_straight_into_a_playlist(client, ctl):
    client.post("/api/saved/new", json={"name": "Inbox"})
    post_file(client, "a.mp4", MP4, to="Inbox")
    post_file(client, "s.mp3", MP3, to="Inbox")
    saved = state(ctl)["saved"]["Inbox"]
    assert (saved["playlist"], saved["sounds"]) == (["a.mp4"], ["s.mp3"])
    assert state(ctl)["playlist"] == []  # nothing changed on the frame


def test_settings_from_before_active_existed(client, ctl, cfg):
    post_file(client, "a.mp4", MP4)
    entry = {"playlist": ["a.mp4"], "interval": 300, "shuffle": False, "sounds": [],
             "sound_interval": 0, "sound_shuffle": False, "fade": 1.0}
    cfg.state_file.write_text(json.dumps({"playlist": ["a.mp4"], "saved": {"Old": entry}}))
    assert client.get("/api/status").get_json()["active"] == "Old"
    res = client.post("/api/saved/edit", json={"name": "Old", "interval": 60})
    assert res.get_json()["playing"] is True and state(ctl)["interval"] == 60


def test_space_endpoint(client, cfg):
    data = client.get("/api/space").get_json()
    assert data["free_bytes"] > 0
    assert data["reserve_bytes"] == cfg.reserve_mb * 1024 * 1024
    assert data["max_upload_bytes"] == cfg.max_upload_mb * 1024 * 1024



# --- folders --------------------------------------------------------------------


def folders(client):
    return {f["path"]: f for f in client.get("/api/status").get_json()["folders"]}


def test_folders_create_move_rename_delete(client, ctl):
    for name, data in [("a.mp4", MP4), ("b.png", PNG), ("s.mp3", MP3)]:
        post_file(client, name, data)
    assert client.post("/api/folders", json={"name": "Japan"}).get_json()["path"] == "Japan"
    assert client.post("/api/folders", json={"parent": "Japan", "name": " Night "}
                       ).get_json()["path"] == "Japan/Night"
    assert client.post("/api/folders", json={"name": "Japan"}).status_code == 409
    assert client.post("/api/folders", json={"parent": "Nope", "name": "x"}).status_code == 404
    assert client.post("/api/folders", json={"name": ""}).status_code == 400

    res = client.post("/api/media/move",
                      json={"files": ["a.mp4", "s.mp3"], "folder": "Japan/Night"})
    assert res.get_json()["moved"] == 2
    client.post("/api/media/move", json={"files": ["b.png"], "folder": "Japan"})
    media = {m["name"]: m["folder"] for m in client.get("/api/media").get_json()["media"]}
    assert media == {"a.mp4": "Japan/Night", "b.png": "Japan", "s.mp3": "Japan/Night"}
    f = folders(client)
    assert (f["Japan"]["count"], f["Japan"]["visuals"], f["Japan"]["audio"]) == (3, 2, 1)
    assert f["Japan/Night"]["parent"] == "Japan" and f["Japan/Night"]["name"] == "Night"
    assert client.post("/api/media/move", json={"files": ["x.mp4"], "folder": "Japan"}
                       ).status_code == 404
    assert client.post("/api/media/move", json={"files": ["a.mp4"], "folder": "Nope"}
                       ).status_code == 404

    client.post("/api/folders/rename", json={"path": "Japan", "name": "Tokyo"})
    media = {m["name"]: m["folder"] for m in client.get("/api/media").get_json()["media"]}
    assert media["a.mp4"] == "Tokyo/Night" and set(folders(client)) == {"Tokyo", "Tokyo/Night"}

    # Moving a folder takes its files along; it can't go inside itself.
    assert client.post("/api/folders/move", json={"path": "Tokyo", "parent": "Tokyo/Night"}
                       ).status_code == 400
    assert client.post("/api/folders/move", json={"path": "Tokyo/Night", "parent": ""}
                       ).get_json()["path"] == "Night"
    media = {m["name"]: m["folder"] for m in client.get("/api/media").get_json()["media"]}
    assert media["a.mp4"] == "Night" and set(folders(client)) == {"Tokyo", "Night"}
    client.post("/api/folders/move", json={"path": "Night", "parent": "Tokyo"})

    # Deleting a folder keeps the files: they move up a level.
    client.post("/api/folders/delete", json={"path": "Tokyo/Night"})
    media = {m["name"]: m["folder"] for m in client.get("/api/media").get_json()["media"]}
    assert media["a.mp4"] == "Tokyo" and set(folders(client)) == {"Tokyo"}
    assert (ctl.library.dir / "a.mp4").exists()


def test_upload_into_a_folder(client):
    client.post("/api/folders", json={"name": "Inbox"})
    post_file(client, "a.mp4", MP4, folder="Inbox")
    media = {m["name"]: m["folder"] for m in client.get("/api/media").get_json()["media"]}
    assert media == {"a.mp4": "Inbox"}


def test_a_folder_in_a_playlist_stays_linked(client, ctl):
    for name, data in [("a.mp4", MP4), ("b.png", PNG), ("c.png", PNG), ("s.mp3", MP3)]:
        post_file(client, name, data)
    client.post("/api/folders", json={"name": "Art"})
    client.post("/api/media/move", json={"files": ["a.mp4", "s.mp3"], "folder": "Art"})
    client.post("/api/saved/new", json={"name": "Mix"})
    res = client.post("/api/saved/add", json={"name": "Mix", "folder": "Art"})
    assert res.get_json()["added"]
    client.post("/api/saved/add", json={"name": "Mix", "filename": "c.png"})
    entry = state(ctl)["saved"]["Mix"]
    assert entry["playlist"] == ["folder:Art", "c.png"] and entry["sounds"] == ["folder:Art"]
    summary = next(p for p in client.get("/api/status").get_json()["saved"] if p["name"] == "Mix")
    assert (summary["count"], summary["sound_count"]) == (2, 1)

    # Playing it plays the folder's files.
    client.post("/api/saved/load", json={"name": "Mix"})
    st = state(ctl)
    assert (st["playlist"], st["sounds"]) == (["a.mp4", "c.png"], ["s.mp3"])
    # Adding to the folder later adds to what's playing.
    client.post("/api/media/move", json={"files": ["b.png"], "folder": "Art"})
    assert state(ctl)["playlist"] == ["a.mp4", "b.png", "c.png"]
    post_file(client, "d.png", PNG, folder="Art")
    assert state(ctl)["playlist"] == ["a.mp4", "b.png", "d.png", "c.png"]
    # Renaming the folder keeps the link.
    client.post("/api/folders/rename", json={"path": "Art", "name": "Gallery"})
    assert state(ctl)["saved"]["Mix"]["playlist"] == ["folder:Gallery", "c.png"]
    # So does moving it into another folder.
    client.post("/api/folders", json={"name": "Old"})
    assert client.post("/api/folders/move", json={"path": "Gallery", "parent": "Old"}
                       ).get_json()["path"] == "Old/Gallery"
    assert state(ctl)["saved"]["Mix"]["playlist"] == ["folder:Old/Gallery", "c.png"]
    assert state(ctl)["playlist"] == ["a.mp4", "b.png", "d.png", "c.png"]
    client.post("/api/folders/move", json={"path": "Old/Gallery", "parent": ""})
    # Deleting it turns the link into the files it had.
    client.post("/api/folders/delete", json={"path": "Gallery"})
    entry = state(ctl)["saved"]["Mix"]
    assert entry["playlist"] == ["a.mp4", "b.png", "d.png", "c.png"]
    assert entry["sounds"] == ["s.mp3"]
    # Editing with a folder that doesn't exist is refused.
    res = client.post("/api/saved/edit", json={"name": "Mix", "items": ["folder:Nope"]})
    assert res.status_code == 404


def test_deleted_file_leaves_its_folder(client, ctl):
    post_file(client, "a.mp4", MP4)
    client.post("/api/folders", json={"name": "F"})
    client.post("/api/media/move", json={"files": ["a.mp4"], "folder": "F"})
    client.delete("/api/media/a.mp4")
    assert ctl.folders.load()["files"] == {}
    assert folders(client)["F"]["count"] == 0
