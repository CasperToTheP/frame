"""The media library: a flat directory of artwork files.

All filenames that come from the network pass through ``safe_name`` (for new
uploads) or ``resolve`` (for existing files) before touching the filesystem.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

log = logging.getLogger(__name__)

# extension -> kind
EXTENSIONS = {
    ".mp4": "video",
    ".m4v": "video",
    ".mkv": "video",
    ".webm": "video",
    ".mov": "video",
    ".gif": "animation",
    ".jpg": "image",
    ".jpeg": "image",
    ".png": "image",
}

MAX_NAME_LEN = 120


class MediaError(Exception):
    """A user-facing problem with a media file. ``status`` is the HTTP code."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class MediaItem:
    name: str
    kind: str
    size: int
    mtime: float

    def to_dict(self) -> dict:
        return {"name": self.name, "kind": self.kind, "size": self.size, "mtime": self.mtime}


def safe_name(raw: str) -> str:
    """Turn an uploaded filename into a safe, plain filename or raise MediaError.

    Strips any directory part, normalises to ASCII, keeps only
    letters/digits/``._-``, lower-cases the extension and checks it is supported.
    """
    if not raw:
        raise MediaError("missing filename")
    # Drop any client-side directory (both separators, whatever the platform).
    name = re.split(r"[\\/]", raw)[-1]
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    name = re.sub(r"\s+", "_", name.strip())
    name = re.sub(r"[^A-Za-z0-9._-]", "", name)
    # rpartition (not splitext) so ".mp4", left over from e.g. "$$$.mp4", keeps its type.
    stem, dot, ext = name.rpartition(".")
    ext = (dot + ext).lower() if dot else ""
    if ext not in EXTENSIONS:
        allowed = ", ".join(sorted(EXTENSIONS))
        raise MediaError(f"unsupported file type '{ext or '(none)'}'; allowed: {allowed}", 415)
    # No hidden files and nothing that looks like a command-line option.
    stem = stem.strip("._-") or "artwork"
    stem = stem[: MAX_NAME_LEN - len(ext)]
    return stem + ext


def sniff_ok(path: Path, ext: str) -> bool:
    """Cheap magic-number check so obviously wrong files are rejected at upload."""
    try:
        with open(path, "rb") as f:
            head = f.read(16)
    except OSError:
        return False
    kind_checks = {
        ".mp4": lambda h: h[4:8] == b"ftyp",
        ".m4v": lambda h: h[4:8] == b"ftyp",
        ".mov": lambda h: h[4:8] in (b"ftyp", b"moov", b"mdat", b"wide", b"free"),
        ".mkv": lambda h: h[:4] == b"\x1a\x45\xdf\xa3",
        ".webm": lambda h: h[:4] == b"\x1a\x45\xdf\xa3",
        ".gif": lambda h: h[:4] == b"GIF8",
        ".png": lambda h: h[:8] == b"\x89PNG\r\n\x1a\n",
        ".jpg": lambda h: h[:3] == b"\xff\xd8\xff",
        ".jpeg": lambda h: h[:3] == b"\xff\xd8\xff",
    }
    check = kind_checks.get(ext)
    return bool(check and check(head))


class MediaLibrary:
    def __init__(self, media_dir: Path, incoming_dir: Path | None = None):
        self.dir = Path(media_dir)
        self.incoming = Path(incoming_dir) if incoming_dir else self.dir / ".incoming"

    def ensure_dirs(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self.incoming.mkdir(parents=True, exist_ok=True)

    def clean_incoming(self) -> None:
        """Remove partial uploads left behind by a crash or power cut."""
        if not self.incoming.is_dir():
            return
        for p in self.incoming.iterdir():
            try:
                p.unlink()
                log.info("removed stale partial upload %s", p.name)
            except OSError:
                pass

    def list(self) -> list[MediaItem]:
        items = []
        try:
            entries = list(os.scandir(self.dir))
        except FileNotFoundError:
            return []
        for e in entries:
            if e.name.startswith(".") or not e.is_file(follow_symlinks=False):
                continue
            kind = EXTENSIONS.get(os.path.splitext(e.name)[1].lower())
            if not kind:
                continue
            st = e.stat(follow_symlinks=False)
            items.append(MediaItem(e.name, kind, st.st_size, st.st_mtime))
        return sorted(items, key=lambda i: i.name.lower())

    def resolve(self, name: str) -> Path:
        """Path of an existing library file. Rejects anything that isn't a plain name."""
        if (
            not isinstance(name, str)
            or not name
            or name.startswith(".")
            or "/" in name
            or "\\" in name
            or "\x00" in name
            or name != os.path.basename(name)
        ):
            raise MediaError("invalid filename")
        path = self.dir / name
        # Belt and braces: the resolved path must sit directly in the media dir.
        if path.resolve().parent != self.dir.resolve():
            raise MediaError("invalid filename")
        if not path.is_file() or path.is_symlink():
            raise MediaError(f"no such media file: {name}", 404)
        return path

    def exists(self, name: str | None) -> bool:
        if not name:
            return False
        try:
            self.resolve(name)
            return True
        except MediaError:
            return False

    def new_incoming_file(self) -> BinaryIO:
        """Open a temp file for an upload in progress (on the media filesystem)."""
        self.incoming.mkdir(parents=True, exist_ok=True)
        path = self.incoming / (secrets.token_hex(8) + ".part")
        return open(path, "w+b")  # noqa: SIM115 - caller owns the handle

    def add(self, tmp_path: Path, raw_name: str) -> str:
        """Move a finished upload into the library. Returns the final filename.

        Always removes ``tmp_path`` if the file is rejected.
        """
        try:
            name = safe_name(raw_name)
            ext = os.path.splitext(name)[1]
            if os.path.getsize(tmp_path) == 0:
                raise MediaError("uploaded file is empty")
            if not sniff_ok(tmp_path, ext):
                raise MediaError(
                    f"file content does not look like a valid {ext} file", 415
                )
            final = self._reserve_unique(name)
        except BaseException:
            _unlink_quiet(tmp_path)
            raise
        os.replace(tmp_path, final)
        return final.name

    def _reserve_unique(self, name: str) -> Path:
        """Atomically claim ``name`` or ``name-1``, ``name-2``... in the media dir."""
        stem, ext = os.path.splitext(name)
        for i in range(0, 1000):
            candidate = self.dir / (name if i == 0 else f"{stem}-{i}{ext}")
            try:
                fd = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                continue
            os.close(fd)
            return candidate
        raise MediaError("too many files with the same name", 409)

    def delete(self, name: str) -> None:
        path = self.resolve(name)
        path.unlink()


def _unlink_quiet(path: Path) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass
