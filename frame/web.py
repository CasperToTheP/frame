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

from flask import (
    Flask,
    Request,
    abort,
    current_app,
    jsonify,
    render_template,
    request,
    send_file,
)
from werkzeug.exceptions import HTTPException

from . import __version__
from .config import Config, setup_logging
from .controller import Controller
from .media import EXTENSIONS, MediaError, kind_of
from .player import PlayerError, PlayerUnavailable
from .state import FADES, FIT_MODES, ROTATIONS, SCALING_MODES, StateError

log = logging.getLogger("frame.web")

# Choices offered in the UI (the API accepts any number of seconds).
INTERVALS = [(15, "15 seconds"), (30, "30 seconds"), (60, "1 minute"), (120, "2 minutes"),
             (300, "5 minutes"), (600, "10 minutes"), (1800, "30 minutes"), (3600, "1 hour"),
             (0, "Full length (images: 1 minute)")]
SOUND_INTERVALS = [(0, "End of each track"), (60, "1 minute"), (300, "5 minutes"),
                   (600, "10 minutes"), (1800, "30 minutes"), (3600, "1 hour")]


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
    # Thumbnails for files that don't have one yet are made in the background.
    ctl.thumbs.clean()
    ctl.thumbs.request(item.name for item in ctl.library.list())

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
        media = ctl.media()
        return render_template(
            "index.html",
            status=ctl.status(),
            media=media,
            media_index={m["name"]: m for m in media},
            kinds={m["name"]: m["kind"] for m in media},
            folder_index={f["path"]: f for f in ctl.folder_summary()},
            rotations=ROTATIONS,
            fit_modes=FIT_MODES,
            scaling_modes=SCALING_MODES,
            fades=FADES,
            intervals=INTERVALS,
            sound_intervals=SOUND_INTERVALS,
            accept=",".join(sorted(EXTENSIONS)),
            max_upload_mb=cfg.max_upload_mb,
            free_bytes=_free_bytes(cfg.media_dir),
            version=__version__,
        )

    @app.get("/thumb/<path:name>")
    def thumbnail(name: str):
        ctl.library.resolve(name)  # 400/404 for bad or missing names
        path = ctl.thumbs.ready(name)
        if path is None:
            ctl.thumbs.request([name])
            # Not made yet (or impossible): the page shows a placeholder and retries.
            response = jsonify(error="no thumbnail yet",
                               pending=not ctl.thumbs.failed(name))
            response.status_code = 404
            response.headers["Cache-Control"] = "no-store"
            return response
        # The URL carries ?v=<file mtime>, so it can be cached for long.
        return send_file(path, mimetype="image/jpeg", conditional=True, max_age=30 * 86400)

    @app.get("/media/<path:name>")
    def media_file(name: str):
        # The original file, for previews in the UI. Range requests let a phone
        # read just the start of a video for its first frame.
        path = ctl.library.resolve(name)
        response = send_file(path, conditional=True, max_age=3600)
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    # --- API ----------------------------------------------------------------------

    @app.get("/api/status")
    def api_status():
        return jsonify(ctl.status())

    @app.get("/api/space")
    def api_space():
        # The page checks this before uploading: the web server stores a whole
        # upload before Frame sees it, so the check after it arrives comes late.
        return jsonify(free_bytes=_free_bytes(cfg.media_dir),
                       reserve_bytes=cfg.reserve_mb * 1024 * 1024,
                       max_upload_bytes=cfg.max_upload_mb * 1024 * 1024)

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
                f"not enough space on the frame: needs {length // 1048576} MB, "
                f"{max(0, free - cfg.reserve_mb * 1048576) // 1048576} MB free", 507
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
        ctl.thumbs.request(saved)
        result: dict[str, Any] = {"saved": saved}
        heavy = []
        for name in saved:
            warning = ctl.heavy_warning(name) if kind_of(name) != "audio" else None
            if warning:
                log.warning("uploaded %s: %s", name, warning)
                heavy.append(f"{name}: {warning}" if len(saved) > 1 else warning)
        folder = request.form.get("folder")
        if folder:
            # Uploaded while looking at a folder: file them there.
            ctl.move_files(saved, folder)
        target = request.form.get("to")
        if target:
            # Add to a saved playlist (artwork and music go to their own lists).
            for name in saved:
                ctl.add_to_playlist(target, name)
        elif request.form.get("add") in ("1", "true", "on"):
            # Add to the playlist (visuals) or the sound list (audio).
            for name in saved:
                result.update(ctl.add(name))
        elif request.form.get("play") in ("1", "true", "on") and saved:
            last = saved[-1]
            if kind_of(last) == "audio":
                result.update(ctl.set_soundtrack(last))
            else:
                result.update(ctl.play(last))
        if heavy:
            if "warning" in result:
                heavy.append(result["warning"])
            result["warning"] = " ".join(heavy)
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

    @app.post("/api/soundtrack")
    def api_soundtrack():
        # {"filename": "song.mp3"} plays it instead of the artwork's own sound;
        # {"filename": null} goes back to the artwork's own sound.
        name = _json_body().get("filename")
        if name is not None and not isinstance(name, str):
            raise MediaError("filename must be a string or null")
        return jsonify(ok=True, **ctl.set_soundtrack(name))

    @app.post("/api/add")
    def api_add():
        name = _json_body().get("filename")
        if not isinstance(name, str):
            raise MediaError("filename is required")
        return jsonify(ok=True, **ctl.add(name))

    @app.post("/api/playlist")
    def api_playlist():
        # Any of {"items": [...], "interval": seconds (0 = full length), "shuffle": bool}
        return jsonify(ctl.set_playlist(_json_body()))

    @app.post("/api/sounds")
    def api_sounds():
        # Any of {"items": [...], "interval": seconds (0 = whole track), "shuffle": bool}
        return jsonify(ctl.set_sounds(_json_body()))

    @app.post("/api/saved")
    def api_save_playlist():
        # {"name": "Sleeping"}: save the current playlist + sounds under that name.
        return jsonify(ok=True, **ctl.save_playlist(_json_body().get("name")))

    @app.post("/api/saved/new")
    def api_new_playlist():
        # {"name": "Sleeping"}: a new, empty playlist. Nothing on the frame changes.
        return jsonify(ok=True, **ctl.create_playlist(_json_body().get("name")))

    @app.post("/api/saved/edit")
    def api_edit_playlist():
        # {"name": "Sleeping", "items": [...], "sounds": [...], "interval": 600,
        #  "shuffle": false, "sound_interval": 0, "sound_shuffle": false, "fade": 2,
        #  "rename": "Night"} - any of them. The frame follows if it's playing.
        body = dict(_json_body())
        name = body.pop("name", None)
        return jsonify(ok=True, **ctl.edit_playlist(name, body))

    @app.post("/api/saved/add")
    def api_add_to_playlist():
        body = _json_body()
        # {"name": ..., "filename": "a.mp4"} or {"name": ..., "folder": "Japan"}
        return jsonify(ok=True, **ctl.add_to_playlist(body.get("name"), body.get("filename"),
                                                      body.get("folder")))

    @app.post("/api/folders")
    def api_create_folder():
        # {"parent": "Japan", "name": "Night"} - parent "" or missing = top level.
        body = _json_body()
        return jsonify(ok=True, **ctl.create_folder(body.get("parent", ""), body.get("name")))

    @app.post("/api/folders/rename")
    def api_rename_folder():
        body = _json_body()
        return jsonify(ok=True, **ctl.rename_folder(body.get("path"), body.get("name")))

    @app.post("/api/folders/move")
    def api_move_folder():
        # {"path": "Japan/Night", "parent": ""} - "" = the top of the library.
        body = _json_body()
        return jsonify(ok=True, **ctl.move_folder(body.get("path"), body.get("parent")))

    @app.post("/api/folders/delete")
    def api_delete_folder():
        # The folder's files move up a level; nothing is deleted from the SD card.
        return jsonify(ok=True, **ctl.delete_folder(_json_body().get("path")))

    @app.post("/api/media/move")
    def api_move_media():
        # {"files": ["a.mp4", ...], "folder": "Japan/Night"} - folder "" = top level.
        body = _json_body()
        return jsonify(ok=True, **ctl.move_files(body.get("files"), body.get("folder", "")))

    @app.post("/api/saved/load")
    def api_load_playlist():
        return jsonify(ok=True, **ctl.load_playlist(_json_body().get("name")))

    @app.delete("/api/saved/<path:name>")
    def api_delete_playlist(name: str):
        ctl.delete_playlist(name)
        return jsonify(deleted=name)

    @app.post("/api/next")
    def api_next():
        # {"which": "visual"} or {"which": "sound"}
        # Optional "filename": jump straight to that item of the list.
        body = _json_body()
        return jsonify(ok=True, **ctl.next(body.get("which", "visual"), body.get("filename")))

    @app.post("/api/stop")
    def api_stop():
        ctl.stop()
        return jsonify(ok=True)

    @app.post("/api/start")
    def api_start():
        ctl.start()
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
