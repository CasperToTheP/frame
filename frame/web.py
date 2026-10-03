"""frame-web.service: the phone-friendly management UI and its JSON API.

Served by waitress (a small pure-Python production WSGI server).
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from flask import Flask, Request, abort, current_app, jsonify, render_template, request
from werkzeug.exceptions import HTTPException

from . import __version__
from .config import Config, setup_logging
from .controller import Controller
from .media import EXTENSIONS, MediaError
from .player import PlayerError, PlayerUnavailable
from .state import FIT_MODES, ROTATIONS, StateError

log = logging.getLogger("frame.web")


class UploadRequest(Request):
    """Stream uploaded files straight into the media filesystem.

    Werkzeug normally spools uploads to the system temp dir (RAM on systems with
    a tmpfs /tmp). Writing them next to the media directory keeps memory use flat
    and lets the finished file be moved into place with a rename.
    """

    def _get_file_stream(self, total_content_length, content_type, filename=None,
                         content_length=None):
        f = _controller().library.new_incoming_file()
        self.environ.setdefault("frame.tmp_files", []).append(f.name)
        return f


def _controller() -> Controller:
    return current_app.extensions["frame"]


def _json_body() -> dict[str, Any]:
    # Requiring application/json also means browsers must send a CORS preflight,
    # which blocks other websites from silently driving the frame.
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        abort(400, description="expected a JSON object body (Content-Type: application/json)")
    return data


def create_app(cfg: Config | None = None, controller: Controller | None = None) -> Flask:
    cfg = cfg or Config.from_env()
    app = Flask(__name__)
    app.request_class = UploadRequest
    app.config["MAX_CONTENT_LENGTH"] = cfg.max_upload_mb * 1024 * 1024
    app.config["FRAME"] = cfg
    ctl = controller or Controller(cfg)
    app.extensions["frame"] = ctl

    ctl.library.ensure_dirs()
    ctl.library.clean_incoming()

    @app.before_request
    def check_access():
        # The single place to add authentication later (see README "Security").
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("Origin")
            if origin and urlsplit(origin).netloc != request.host:
                abort(403, description="cross-origin request refused")

    @app.teardown_request
    def remove_unclaimed_uploads(_exc):
        # Uploads that were rejected or interrupted leave their temp file behind.
        for name in request.environ.get("frame.tmp_files", []):
            try:
                os.unlink(name)
            except OSError:
                pass

    @app.errorhandler(MediaError)
    def media_error(exc: MediaError):
        return jsonify(error=str(exc)), exc.status

    @app.errorhandler(StateError)
    def state_error(exc: StateError):
        return jsonify(error=str(exc)), 400

    @app.errorhandler(PlayerUnavailable)
    def player_unavailable(exc: PlayerUnavailable):
        return jsonify(error="The player is not running right now. It restarts "
                             "automatically; try again in a few seconds."), 503

    @app.errorhandler(PlayerError)
    def player_error(exc: PlayerError):
        return jsonify(error=f"player error: {exc}"), 502

    @app.errorhandler(HTTPException)
    def http_error(exc: HTTPException):
        if exc.code == 413:
            msg = f"file too large (limit {cfg.max_upload_mb} MB)"
        else:
            msg = exc.description or exc.name
        if request.path.startswith("/api/"):
            return jsonify(error=msg), exc.code
        return f"{exc.code} {msg}", exc.code

    # --- pages ----------------------------------------------------------------

    @app.get("/")
    def index():
        return render_template(
            "index.html",
            status=ctl.status(),
            media=ctl.media(),
            rotations=ROTATIONS,
            fit_modes=FIT_MODES,
            accept=",".join(sorted(EXTENSIONS)),
            max_upload_mb=cfg.max_upload_mb,
            version=__version__,
        )

    # --- API ----------------------------------------------------------------------

    @app.get("/api/status")
    def api_status():
        return jsonify(ctl.status())

    @app.get("/api/media")
    def api_media():
        return jsonify(media=ctl.media(), free_bytes=_free_bytes(cfg.media_dir))

    @app.post("/api/media")
    def api_upload():
        length = request.content_length
        if length is None:
            abort(411, description="upload size unknown (missing Content-Length)")
        free = _free_bytes(cfg.media_dir)
        if free is not None and length + cfg.reserve_mb * 1024 * 1024 > free:
            raise MediaError(
                f"not enough free space on the SD card ({free // (1024 * 1024)} MB free)", 507
            )
        files = request.files.getlist("file")
        if not files:
            raise MediaError("no file in upload (form field 'file')")
        saved = []
        for storage in files:
            tmp = Path(storage.stream.name) if hasattr(storage.stream, "name") else None
            storage.stream.close()
            if tmp is None or not tmp.exists():
                raise MediaError("upload could not be stored")
            name = ctl.library.add(tmp, storage.filename or "")
            log.info("uploaded %s (%d bytes)", name, (cfg.media_dir / name).stat().st_size)
            saved.append(name)
        result: dict[str, Any] = {"saved": saved}
        if request.form.get("play") in ("1", "true", "on") and saved:
            result.update(ctl.play(saved[-1]))
        return jsonify(result), 201

    @app.delete("/api/media/<path:name>")
    def api_delete(name: str):
        force = request.args.get("force") in ("1", "true")
        ctl.delete(name, force=force)
        return jsonify(deleted=name)

    @app.post("/api/play")
    def api_play():
        name = _json_body().get("filename")
        if not isinstance(name, str):
            raise MediaError("filename is required")
        return jsonify(ok=True, **ctl.play(name))

    @app.post("/api/stop")
    def api_stop():
        ctl.stop()
        return jsonify(ok=True)

    @app.post("/api/pause")
    def api_pause():
        ctl.pause()
        return jsonify(ok=True)

    @app.post("/api/resume")
    def api_resume():
        ctl.resume()
        return jsonify(ok=True)

    @app.post("/api/volume")
    def api_volume():
        return jsonify(volume=ctl.set_volume(_json_body().get("volume")))

    @app.post("/api/mute")
    def api_mute():
        # {"muted": true/false} sets it; an empty object toggles.
        body = request.get_json(silent=True)
        if body is None and request.content_length:
            abort(400, description="expected a JSON object body")
        muted = (body or {}).get("muted")
        if muted is not None and not isinstance(muted, bool):
            raise StateError("muted must be true or false")
        return jsonify(muted=ctl.set_muted(muted))

    @app.post("/api/settings")
    def api_settings():
        return jsonify(ctl.update_settings(_json_body()))

    @app.get("/api/audio-devices")
    def api_audio_devices():
        return jsonify(devices=ctl.audio_devices())

    return app


def _free_bytes(path: Path) -> int | None:
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None


def main() -> int:
    setup_logging()
    cfg = Config.from_env()
    from waitress import serve

    app = create_app(cfg)
    log.info("frame web UI %s listening on http://%s:%d", __version__, cfg.host, cfg.port)
    serve(
        app,
        host=cfg.host,
        port=cfg.port,
        threads=4,
        # waitress buffers request bodies to temp files above this size (TMPDIR).
        max_request_body_size=(cfg.max_upload_mb + 1) * 1024 * 1024,
        channel_timeout=300,  # slow phone uploads over Wi-Fi
        ident="frame",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
