"""The playlist logic, with fake players and a fake clock (no waiting)."""

import os
import random

import pytest

from frame.director import BLACK, IMAGE_SECONDS, Director
from frame.state import StateStore

from .conftest import MP3, MP4, PNG, FakeIpc


class Clock:
    def __init__(self):
        self.t = 1000.0

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


class Player(FakeIpc):
    """FakeIpc that also writes to a log shared by both players, to check ordering."""

    def __init__(self, label, log):
        super().__init__()
        self.label, self.log = label, log
        self.props.update({"brightness": 0, "volume": 70, "aid": "auto", "current-ao": "alsa",
                           "duration": None})

    def set(self, name, value):
        super().set(name, value)
        self.log.append((self.label, "set", name, value))
        if name == "aid":
            self.props["current-ao"] = None if value == "no" else "alsa"

    def command(self, *args):
        super().command(*args)
        self.log.append((self.label, *args))

    def load(self, path, wait=6.0):
        self.log.append((self.label, "load", path.name))
        super().load(path, wait)


@pytest.fixture
def env(cfg):
    cfg.media_dir.mkdir(parents=True)
    for name, data in [("a.mp4", MP4), ("b.mp4", MP4), ("c.png", PNG), ("s1.mp3", MP3),
                       ("s2.mp3", MP3)]:
        (cfg.media_dir / name).write_bytes(data)
    log = []
    clock = Clock()
    store = StateStore(cfg.state_file)
    stamp = [0]

    def set_state(**changes):
        store.update(**changes)
        stamp[0] += 1  # make sure the director sees a new mtime, however coarse the fs clock
        os.utime(cfg.state_file, ns=(stamp[0] * 10**9, stamp[0] * 10**9))

    set_state(fade=0.0)
    video, audio = Player("video", log), Player("audio", log)
    audio.props.update({"idle-active": True, "path": None})

    def make():
        d = Director(cfg, video, audio, sleep=clock.sleep, clock=clock.now,
                     rng=random.Random(4))
        return d

    class Env:
        pass

    e = Env()
    e.cfg, e.log, e.clock, e.video, e.audio, e.set_state, e.make = (
        cfg, log, clock, video, audio, set_state, make)
    return e


def run(env, director, seconds, step=0.5):
    end = env.clock.t + seconds
    while env.clock.t < end:
        env.clock.t += step
        director.tick()


def loads(env, label="video"):
    return [entry[2] for entry in env.log if entry[0] == label and entry[1] == "load"]


def start(env, **state):
    env.set_state(**state)
    d = env.make()
    visual, sound = d.initial()
    # The supervisor starts mpv with these files on its command line.
    if visual:
        env.video.props["path"] = str(visual)
    if sound:
        env.audio.props.update({"path": str(sound), "idle-active": False})
    return d, visual, sound


def test_single_artwork_loops_forever(env):
    d, visual, sound = start(env, playlist=["a.mp4"], interval=15)
    assert visual.name == "a.mp4" and sound is None
    run(env, d, 3600)
    assert loads(env) == []


def test_playlist_advances_in_order_and_wraps(env):
    d, visual, _ = start(env, playlist=["a.mp4", "b.mp4", "c.png"], interval=60)
    assert visual.name == "a.mp4"
    run(env, d, 59)
    assert loads(env) == []
    run(env, d, 2)
    assert loads(env) == ["b.mp4"]
    run(env, d, 120)
    assert loads(env) == ["b.mp4", "c.png", "a.mp4"]


def test_fade_goes_through_black_and_back(env):
    d, _, _ = start(env, playlist=["a.mp4", "b.mp4"], interval=30, fade=1.0)
    run(env, d, 30)
    def brightness(entries):
        return [e[3] for e in entries if e[0] == "video" and e[1:3] == ("set", "brightness")]

    load_at = env.log.index(("video", "load", "b.mp4"))
    before = brightness(env.log[:load_at])
    brightness = brightness(env.log)
    # Fades out to black, loads, fades back in, ends fully visible.
    assert min(brightness) == BLACK and brightness[-1] == 0
    assert before and before[-1] == BLACK
    assert len(brightness) > 40  # 25 steps per second, both ways
    # The artwork's own sound fades with it, and ends at the saved volume.
    volumes = [e[3] for e in env.log if e[0] == "video" and e[1] == "set" and e[2] == "volume"]
    assert 0 in volumes and volumes[-1] == 70
    # The fade-out starts before the interval is over, so each item gets its full time.
    assert env.clock.t - 1000 <= 32


def test_full_length_uses_the_file_duration(env):
    env.video.props["duration"] = 12.0
    d, _, _ = start(env, playlist=["a.mp4", "b.mp4"], interval=0)
    run(env, d, 11)
    assert loads(env) == []
    run(env, d, 1.5)
    assert loads(env) == ["b.mp4"]


def test_full_length_images_stay_a_minute(env):
    d, _, _ = start(env, playlist=["c.png", "a.mp4"], interval=0)
    run(env, d, IMAGE_SECONDS - 1)
    assert loads(env) == []
    run(env, d, 2)
    assert loads(env) == ["a.mp4"]


def test_choice_from_the_web_ui_applies_at_once(env):
    d, _, _ = start(env, playlist=["a.mp4", "b.mp4"], interval=600)
    env.video.props["pause"] = True
    env.set_state(playlist=["c.png"])
    d.tick()
    assert loads(env) == ["c.png"]
    assert ("video", "set", "pause", False) in env.log  # a new choice always plays


def test_editing_the_list_keeps_the_current_item(env):
    d, _, _ = start(env, playlist=["a.mp4", "b.mp4"], interval=600)
    env.set_state(playlist=["b.mp4", "a.mp4", "c.png"])
    d.tick()
    assert loads(env) == []
    assert d.visual.current == "a.mp4"


def test_next_request(env):
    d, _, _ = start(env, playlist=["a.mp4", "b.mp4"], interval=600)
    env.cfg.run_dir.mkdir(parents=True, exist_ok=True)
    (env.cfg.run_dir / "next-visual").touch()
    d.tick()
    assert loads(env) == ["b.mp4"]
    assert not (env.cfg.run_dir / "next-visual").exists()


def test_paused_means_no_switching(env):
    d, _, _ = start(env, playlist=["a.mp4", "b.mp4"], interval=60)
    env.video.props["pause"] = True
    run(env, d, 300)
    assert loads(env) == []
    env.video.props["pause"] = False
    run(env, d, 61)
    assert loads(env) == ["b.mp4"]


def test_shuffle_never_repeats_the_current_item(env):
    d, _, _ = start(env, playlist=["a.mp4", "b.mp4", "c.png"], interval=15, shuffle=True)
    seen = [d.visual.current]
    for _ in range(600):
        run(env, d, 0.5)
        if d.visual.current != seen[-1]:
            seen.append(d.visual.current)
    assert len(seen) >= 15  # it kept switching
    assert set(seen) == {"a.mp4", "b.mp4", "c.png"}
    # (seen only records changes, so a repeat would have shown up as a missing switch)
    assert len(seen) - 1 == len(loads(env))


def test_broken_file_is_skipped(env):
    env.video.invalid.add("b.mp4")
    d, _, _ = start(env, playlist=["a.mp4", "b.mp4", "c.png"], interval=60)
    run(env, d, 61)
    assert loads(env) == ["b.mp4", "c.png"]
    assert "b.mp4" in d.error
    assert d.status()["visual"]["count"] == 2


def test_black_screen_stops_both_players(env):
    d, _, sound = start(env, playlist=["a.mp4"], sounds=["s1.mp3"])
    assert sound.name == "s1.mp3"
    env.set_state(blank=True)
    d.tick()
    assert ("video", "stop") in env.log and ("audio", "stop") in env.log
    assert d.visual.current is None and d.sound.current is None


def test_adding_sound_releases_the_device_first(env):
    d, _, _ = start(env, playlist=["a.mp4"])
    env.set_state(sounds=["s1.mp3"])
    d.tick()
    aid = env.log.index(("video", "set", "aid", "no"))
    load = env.log.index(("audio", "load", "s1.mp3"))
    assert aid < load
    assert d.sound.current == "s1.mp3"


def test_removing_sound_gives_the_device_back(env):
    d, _, _ = start(env, playlist=["a.mp4"], sounds=["s1.mp3"])
    env.set_state(sounds=[])
    d.tick()
    stop = env.log.index(("audio", "stop"))
    aid = env.log.index(("video", "set", "aid", "auto"))
    assert stop < aid


def test_sounds_change_on_their_own_clock(env):
    env.audio.props["duration"] = 100.0
    d, _, sound = start(env, playlist=["a.mp4", "b.mp4"], interval=30,
                        sounds=["s1.mp3", "s2.mp3"], sound_interval=0, fade=1.0)
    assert sound.name == "s1.mp3"
    run(env, d, 100)
    assert loads(env, "audio") == ["s2.mp3"]
    assert len(loads(env)) == 3  # the visuals kept their own 30 s rhythm
    volumes = [e[3] for e in env.log if e[0] == "audio" and e[2:3] == ("volume",)]
    assert 0 in volumes and volumes[-1] == 70
    # With a soundtrack, a visual fade leaves the artwork's (muted) sound alone.
    assert not any(e[0] == "video" and e[2:3] == ("volume",) and e[3] == 0 for e in env.log
                   if e[1] == "set")


def test_status_counts_down(env):
    d, _, _ = start(env, playlist=["a.mp4", "b.mp4"], interval=60)
    run(env, d, 10)
    st = d.status()
    assert st["visual"]["position"] == 1 and st["visual"]["count"] == 2
    assert st["visual"]["next_at"] is not None
    assert st["sound"]["current"] is None


def test_fade_problems_never_stop_the_playlist(env):
    original = env.video.set

    def no_brightness(name, value):
        if name == "brightness":
            from frame.player import PlayerError
            raise PlayerError("set_property: error accessing property")
        original(name, value)

    env.video.set = no_brightness
    d, _, _ = start(env, playlist=["a.mp4", "b.mp4"], interval=30, fade=1.0)
    run(env, d, 31)
    assert loads(env) == ["b.mp4"]
    assert d.error is None


def test_hang_while_loading_names_the_file_being_opened(env):
    from frame.player import PlayerUnavailable

    d, _, _ = start(env, playlist=["a.mp4", "b.mp4"], interval=30)

    def hang(path, wait=6.0):
        raise PlayerUnavailable("player did not answer in time")

    env.video.load = hang
    run(env, d, 31)
    assert d.visual.current == "a.mp4" and d.showing() == "b.mp4"
    d.initial()  # the supervisor restarts mpv
    assert d.showing() == "a.mp4"
