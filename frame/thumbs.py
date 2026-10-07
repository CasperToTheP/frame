"""Small JPEG thumbnails of the artwork, for the web UI.

Phones can't be relied on to show a video's first frame by themselves (iOS
shows nothing until a video plays), so the frame makes one 480-px-wide JPEG per
visual, once, with the mpv that's already installed (``--vo=image``; no ffmpeg).

Thumbnails are made by one background thread, one file at a time, at the lowest
CPU priority, so they never hold up a web request or the playback. A request
for a thumbnail that doesn't exist yet gets a 404 straight away and queues it;
the page retries a few seconds later.

Memory: decoding one frame takes ~70 MB (~160 MB for 4K) in software. The mpv
runs inside frame-web's cgroup (MemoryMax), with its OOM score raised, so if a
file is too big it's the thumbnailer that gets killed, never the player.
"""

from __future__ import annotations

import logging
import os
import queue
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Callable, Iterable
from pathlib import Path

from .media import VISUAL_KINDS, MediaError, MediaLibrary, kind_of

log = logging.getLogger("frame.thumbs")

WIDTH = 480
TIMEOUT = 90  # seconds per file; a Pi 4 needs a few seconds for a 1080p video


def mpv_thumbnail_args(mpv_bin: str, src: Path, outdir: Path, kind: str) -> list[str]:
    # Videos: a frame 10% in, past fade-ins and black first frames.
    start = "10%" if kind == "video" else "0"
    return [
        mpv_bin, "--no-config", "--really-quiet", "--no-audio", "--ao=null",
        "--vo=image", "--vo-image-format=jpg", "--vo-image-jpeg-quality=82",
        f"--vo-image-outdir={outdir}", "--frames=1", f"--start={start}",
        "--hwdec=no", "--vd-lavc-threads=1", "--image-display-duration=0",
        f"--vf=lavfi=[scale={WIDTH}:-2]", "--", str(src),
    ]


def _lower_priority() -> None:  # runs in the child before exec
    try:
        os.nice(19)
    except OSError:
        pass
    try:
        with open("/proc/self/oom_score_adj", "w") as f:
            f.write("1000")
    except OSError:
        pass


def run_mpv(args: list[str]) -> None:
    subprocess.run(args, check=True, timeout=TIMEOUT, stdin=subprocess.DEVNULL,
                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                   preexec_fn=_lower_priority if os.name == "posix" else None)


def _describe(exc: Exception) -> str:
    if isinstance(exc, subprocess.TimeoutExpired):
        return f"mpv took longer than {TIMEOUT} s"
    if isinstance(exc, subprocess.CalledProcessError):
        lines = (exc.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        return f"mpv failed (exit {exc.returncode})" + (f": {lines[-1]}" if lines else "")
    return str(exc)


class Thumbnails:
    def __init__(self, library: MediaLibrary, thumb_dir: Path, mpv_bin: str = "mpv",
                 runner: Callable[[list[str]], None] | None = None):
        self.library = library
        self.dir = Path(thumb_dir)
        self.mpv_bin = mpv_bin
        self._runner = runner
        self._queue: queue.Queue[str] = queue.Queue()
        self._pending: set[str] = set()
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None

    # --- paths ------------------------------------------------------------------

    def path(self, name: str) -> Path:
        return self.dir / f"{name}.jpg"

    def _failed_marker(self, name: str) -> Path:
        return self.dir / f"{name}.failed"

    def _source_mtime(self, name: str) -> float | None:
        try:
            return self.library.resolve(name).stat().st_mtime
        except (MediaError, OSError):
            return None

    def _newer_than_source(self, path: Path, name: str) -> bool:
        src = self._source_mtime(name)
        try:
            return src is not None and path.stat().st_mtime >= src
        except OSError:
            return False

    def ready(self, name: str) -> Path | None:
        """The thumbnail if it exists and is up to date, else None."""
        path = self.path(name)
        return path if self._newer_than_source(path, name) else None

    def failed(self, name: str) -> bool:
        return self._newer_than_source(self._failed_marker(name), name)

    def version(self, name: str) -> int:
        """Changes when the file changes: used to bust the browser cache."""
        return int(self._source_mtime(name) or 0)

    # --- making them --------------------------------------------------------------

    def make(self, name: str) -> Path | None:
        """Make the thumbnail now (blocking). None if the file can't be thumbnailed."""
        kind = kind_of(name)
        if kind not in VISUAL_KINDS:
            return None
        existing = self.ready(name)
        if existing or self.failed(name):
            return existing
        try:
            src = self.library.resolve(name)
        except MediaError:
            return None
        self.dir.mkdir(parents=True, exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix=".make-", dir=self.dir))
        try:
            (self._runner or run_mpv)(mpv_thumbnail_args(self.mpv_bin, src, work, kind))
            made = sorted(work.glob("*.jpg"))
            if not made:
                raise RuntimeError("mpv wrote no image")
            os.replace(made[0], self.path(name))
            self._failed_marker(name).unlink(missing_ok=True)
            log.info("thumbnail made for %s", name)
            return self.path(name)
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            log.warning("no thumbnail for %s: %s", name, _describe(exc))
            try:
                self._failed_marker(name).touch()
            except OSError:
                pass
            return None
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def request(self, names: Iterable[str]) -> None:
        """Queue thumbnails to be made in the background (missing ones only)."""
        with self._lock:
            for name in names:
                if kind_of(name) not in VISUAL_KINDS or name in self._pending:
                    continue
                if self.ready(name) or self.failed(name):
                    continue
                self._pending.add(name)
                self._queue.put(name)
            if self._pending and (self._worker is None or not self._worker.is_alive()):
                self._worker = threading.Thread(target=self._work, name="thumbnails",
                                                daemon=True)
                self._worker.start()

    def _work(self) -> None:
        while True:
            try:
                name = self._queue.get(timeout=5)
            except queue.Empty:
                with self._lock:
                    if self._queue.empty():
                        self._worker = None
                        return
                continue
            try:
                self.make(name)
            except Exception:  # never let one bad file stop the worker
                log.exception("thumbnail for %s failed", name)
            finally:
                with self._lock:
                    self._pending.discard(name)

    def wait(self, timeout: float = 30) -> None:
        """Block until the queue is done (for tests)."""
        worker = self._worker
        if worker is not None:
            worker.join(timeout)

    # --- housekeeping -----------------------------------------------------------

    def remove(self, name: str) -> None:
        for path in (self.path(name), self._failed_marker(name)):
            path.unlink(missing_ok=True)

    def clean(self) -> None:
        """Remove thumbnails of files that no longer exist, and leftover temp dirs."""
        if not self.dir.is_dir():
            return
        for entry in self.dir.iterdir():
            if entry.name.startswith(".make-"):
                shutil.rmtree(entry, ignore_errors=True)
                continue
            for suffix in (".jpg", ".failed"):
                if entry.name.endswith(suffix):
                    name = entry.name[: -len(suffix)]
                    if not self.library.exists(name):
                        entry.unlink(missing_ok=True)
