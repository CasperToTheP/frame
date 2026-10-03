import json
import socket
import threading
from pathlib import Path

import pytest

from frame import display
from frame.player import (
    InvalidMedia,
    MpvIpc,
    PlayerError,
    PlayerUnavailable,
    build_mpv_args,
    fit_properties,
)
from frame.state import DEFAULTS


class FakeSocket:
    """In-memory socket. ``handler(request) -> list[dict]`` produces mpv's replies."""

    def __init__(self, handler):
        self.handler = handler
        self.sent: list[dict] = []
        self.out = b""
        self.closed = False

    def settimeout(self, t):
        pass

    def sendall(self, data):
        for line in data.decode().splitlines():
            req = json.loads(line)
            self.sent.append(req)
            for msg in self.handler(req):
                self.out += json.dumps(msg).encode() + b"\n"

    def recv(self, n):
        if not self.out:
            raise TimeoutError
        chunk, self.out = self.out[:n], self.out[n:]
        return chunk

    def close(self):
        self.closed = True


def make_ipc(handler):
    sockets = []

    def connect(path, timeout):
        s = FakeSocket(handler)
        sockets.append(s)
        return s

    return MpvIpc(Path("/run/frame/mpv.sock"), connect=connect), sockets


def ok(req, data=None):
    return {"request_id": req["request_id"], "error": "success", "data": data}


def test_command_wire_format_and_reply():
    ipc, socks = make_ipc(lambda req: [ok(req, 55)])
    assert ipc.command("get_property", "volume") == 55
    sent = socks[0].sent[0]
    assert sent["command"] == ["get_property", "volume"]
    assert isinstance(sent["request_id"], int)
    assert socks[0].closed


def test_events_and_other_replies_are_skipped():
    def handler(req):
        return [
            {"event": "playback-restart"},
            {"request_id": -1, "error": "success", "data": "wrong"},
            ok(req, True),
        ]

    ipc, _ = make_ipc(handler)
    assert ipc.get("pause") is True


def test_reply_split_across_recv_calls():
    ipc, socks = make_ipc(lambda req: [ok(req, "x" * 200_000)])
    assert ipc.get("path") == "x" * 200_000


def test_error_reply_raises_player_error():
    ipc, _ = make_ipc(lambda req: [{"request_id": req["request_id"], "error": "invalid parameter"}])
    with pytest.raises(PlayerError, match="invalid parameter"):
        ipc.set("volume", "loud")


def test_get_returns_default_for_unavailable_property():
    ipc, _ = make_ipc(
        lambda req: [{"request_id": req["request_id"], "error": "property unavailable"}]
    )
    assert ipc.get("path", "none") == "none"


def test_connect_failure_is_unavailable():
    def connect(path, timeout):
        raise FileNotFoundError(2, "No such file")

    ipc = MpvIpc(Path("/nope"), connect=connect)
    with pytest.raises(PlayerUnavailable):
        ipc.command("get_property", "pid")
    assert ipc.ping() is False


def test_no_reply_is_unavailable():
    ipc, _ = make_ipc(lambda req: [])
    with pytest.raises(PlayerUnavailable):
        ipc.get("pause")


def test_load_waits_for_file_loaded():
    def handler(req):
        return [
            ok(req),
            {"event": "end-file", "reason": "stop"},  # the previous file ending
            {"event": "start-file"},
            {"event": "file-loaded"},
        ]

    ipc, socks = make_ipc(handler)
    ipc.load(Path("/var/lib/frame/media/a.mp4"))
    assert socks[0].sent[0]["command"] == ["loadfile", str(Path("/var/lib/frame/media/a.mp4")),
                                           "replace"]


def test_load_reports_invalid_media():
    def handler(req):
        return [
            ok(req),
            {"event": "start-file"},
            {"event": "end-file", "reason": "error", "file_error": "unrecognized file format"},
        ]

    ipc, _ = make_ipc(handler)
    with pytest.raises(InvalidMedia, match="unrecognized"):
        ipc.load(Path("bad.mp4"))


def test_load_without_confirmation_assumes_ok():
    ipc, _ = make_ipc(lambda req: [ok(req)])
    ipc.load(Path("slow.mp4"), wait=0.05)


# --- command line ------------------------------------------------------------


def test_mpv_args_defaults():
    state = dict(DEFAULTS, current="a.mp4", volume=33, muted=True, rotation=270)
    args = build_mpv_args(
        "mpv", Path("/run/frame/mpv.sock"), state, Path("/m/a.mp4"),
        drm_device="/dev/dri/card1", drm_connector="HDMI-A-1",
        audio_device="alsa/hdmi:CARD=vc4hdmi0,DEV=0",
    )
    assert args[0] == "mpv"
    for flag in [
        "--vo=gpu", "--gpu-context=drm", "--fullscreen", "--idle=yes", "--force-window=yes",
        "--no-osc", "--osd-level=0", "--cursor-autohide=always", "--loop-file=inf",
        "--no-config", "--input-terminal=no", "--audio-fallback-to-null=yes",
        "--volume=33", "--mute=yes", "--video-rotate=270", "--keepaspect=yes",
        "--hwdec=auto-safe", "--drm-device=/dev/dri/card1", "--drm-connector=HDMI-A-1",
        "--audio-device=alsa/hdmi:CARD=vc4hdmi0,DEV=0",
    ]:
        assert flag in args, flag
    assert any(a.startswith("--input-ipc-server=") for a in args)
    # The file comes last, after "--", so a name can never be read as an option.
    assert args[-2:] == ["--", str(Path("/m/a.mp4"))]


def test_mpv_args_without_media_or_display():
    args = build_mpv_args("mpv", Path("s"), dict(DEFAULTS), None)
    assert "--" not in args
    assert not any(a.startswith(("--drm-device", "--drm-connector", "--audio-device"))
                   for a in args)


@pytest.mark.parametrize(
    "fit, expected",
    [
        ("fit", {"keepaspect": True, "panscan": 0.0}),
        ("fill", {"keepaspect": True, "panscan": 1.0}),
        ("stretch", {"keepaspect": False, "panscan": 0.0}),
    ],
)
def test_fit_properties(fit, expected):
    assert fit_properties(fit) == expected


# --- display / audio detection ------------------------------------------------


def fake_sysfs(root, connectors):
    for name, status in connectors.items():
        d = root / name
        d.mkdir(parents=True)
        (d / "status").write_text(status + "\n")
    (root / "card1").mkdir(parents=True, exist_ok=True)  # not a connector
    (root / "renderD128").mkdir(parents=True, exist_ok=True)


def test_pick_connected_hdmi(tmp_path):
    fake_sysfs(tmp_path, {"card1-HDMI-A-1": "disconnected", "card1-HDMI-A-2": "connected",
                          "card1-Composite-1": "unknown"})
    conns = display.list_connectors(tmp_path)
    assert len(conns) == 3
    chosen = display.pick_connector(conns)
    assert (chosen.card, chosen.name, chosen.device) == ("card1", "HDMI-A-2", "/dev/dri/card1")


def test_no_display_connected(tmp_path):
    fake_sysfs(tmp_path, {"card0-HDMI-A-1": "disconnected"})
    assert display.pick_connector(display.list_connectors(tmp_path)) is None


def test_no_sysfs(tmp_path):
    assert display.list_connectors(tmp_path / "missing") == []


def test_hdmi_audio_device(tmp_path):
    asound = tmp_path / "asound"
    (asound / "vc4hdmi0").mkdir(parents=True)
    hdmi1 = display.Connector("card1", "HDMI-A-1", True)
    hdmi2 = display.Connector("card1", "HDMI-A-2", True)
    assert display.hdmi_audio_device(hdmi1, asound) == "alsa/hdmi:CARD=vc4hdmi0,DEV=0"
    assert display.hdmi_audio_device(hdmi2, asound) is None  # card not present
    assert display.hdmi_audio_device(None, asound) is None
    assert display.hdmi_audio_device(display.Connector("c", "DSI-1", True), asound) is None


# --- real unix socket (Linux/macOS only) --------------------------------------


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="needs AF_UNIX sockets")
def test_real_unix_socket_roundtrip(tmp_path):
    path = str(tmp_path / "mpv.sock")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    server.listen(1)

    def serve():
        conn, _ = server.accept()
        with conn:
            req = json.loads(conn.makefile().readline())
            conn.sendall(b'{"event":"idle"}\n')
            reply = {"request_id": req["request_id"], "error": "success", "data": 1234}
            conn.sendall(json.dumps(reply).encode() + b"\n")

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    try:
        assert MpvIpc(Path(path)).get("pid") == 1234
    finally:
        t.join(2)
        server.close()
