"""The media library: a flat directory of artwork files.

All filenames that come from the network pass through ``safe_name`` (for new
uploads) or ``resolve`` (for existing files) before touching the filesystem.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import shutil
import subprocess
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

log = logging.getLogger(__name__)

# extension -> kind. "audio" files are soundtracks; everything else is a visual.
EXTENSIONS = {
    ".mp4": "video",
    ".m4v": "video",
    ".mkv": "video",
    ".webm": "video",
    # Imgur-style "GIF video": really an MP4 or WebM. Renamed on upload (see add()).
    ".gifv": "video",
    ".mov": "video",
    ".gif": "animation",
    ".jpg": "image",
    ".jpeg": "image",
    ".png": "image",
    ".webp": "image",  # still WebP only: ffmpeg can't decode animated WebP
    ".bmp": "image",
    ".tif": "image",
    ".tiff": "image",
    # Converted to PNG on upload (mpv on Bookworm can't open SVG).
    ".svg": "image",
    ".mp3": "audio",
    ".m4a": "audio",
    ".aac": "audio",
    ".wav": "audio",
    ".flac": "audio",
    ".ogg": "audio",
    ".opus": "audio",
}
VISUAL_KINDS = ("video", "animation", "image")

MAX_NAME_LEN = 120
# SVGs are rendered to fit this square, so they are sharp in any rotation.
SVG_RENDER_SIZE = 1920
SVG_TIMEOUT = 60


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


def _is_mp3(h: bytes) -> bool:
    # ID3 tag, or a bare MPEG audio frame sync.
    return h[:3] == b"ID3" or (h[0] == 0xFF and h[1] & 0xE0 == 0xE0)


def _is_svg(h: bytes) -> bool:
    # XML text. The <svg> tag itself may come after a long header or comment.
    text = h.lstrip(b"\xef\xbb\xbf").lstrip().lower()
    return text.startswith((b"<?xml", b"<!--", b"<!doctype")) or text.startswith(b"<svg")


def is_animated_webp(h: bytes) -> bool:
    # Extended WebP ("VP8X" chunk) with the animation flag set.
    return h[12:16] == b"VP8X" and len(h) > 20 and bool(h[20] & 0x02)


def kind_of(name: str) -> str | None:
    return EXTENSIONS.get(os.path.splitext(name)[1].lower())


def rasterize_svg(src: Path, dst: Path) -> None:
    """Render an SVG to a PNG on a black background, or raise MediaError.

    The longest side becomes SVG_RENDER_SIZE pixels. rsvg-convert versions
    disagree on how -w and -h together fit an image, so the SVG is first
    rendered small to measure its shape, then sized by its longest side alone.
    """
    tool = shutil.which("rsvg-convert")
    if not tool:
        raise MediaError("SVG support needs rsvg-convert (sudo apt install librsvg2-bin)", 415)

    def render(*opts: str) -> None:
        try:
            subprocess.run(
                [tool, *opts, "--background-color=black", "-f", "png",
                 "-o", str(dst), str(src)],
                check=True, capture_output=True, timeout=SVG_TIMEOUT,
            )
        except subprocess.TimeoutExpired as exc:
            raise MediaError("SVG took too long to render", 422) from exc
        except subprocess.CalledProcessError as exc:
            detail = exc.stderr.decode("utf-8", "replace").strip().splitlines()[:1]
            raise MediaError(
                f"could not render SVG{': ' + detail[0] if detail else ''}", 422
            ) from exc

    render("--keep-aspect-ratio", "-w", "256")
    with open(dst, "rb") as f:
        head = f.read(24)
    width, height = int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")
    if head[:8] != b"\x89PNG\r\n\x1a\n" or not width or not height:
        raise MediaError("could not render SVG", 422)
    side = "-w" if width >= height else "-h"
    render("--keep-aspect-ratio", side, str(SVG_RENDER_SIZE))


def gifv_real_ext(path: Path) -> str:
    """What a .gifv upload really is: ".mp4" or ".webm", or MediaError.

    A .gifv is a short video named like a GIF. Saving one from a browser
    sometimes saves the web page around it instead, so say how to get the video.
    """
    try:
        with open(path, "rb") as f:
            head = f.read(512)
    except OSError as exc:
        raise MediaError("upload could not be read") from exc
    if head[4:8] == b"ftyp":
        return ".mp4"
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return ".webm"
    if b"<html" in head.lower() or b"<!doctype" in head.lower():
        raise MediaError(
            "this .gifv is a web page, not the video. Change “.gifv” to “.mp4” at the end "
            "of the link, open that, and save the video", 415)
    raise MediaError("this .gifv doesn't contain an MP4 or WebM video", 415)


def sniff_ok(path: Path, ext: str) -> bool:
    """Cheap magic-number check so obviously wrong files are rejected at upload."""
    try:
        with open(path, "rb") as f:
            head = f.read(1024)
    except OSError:
        return False
    if len(head) < 4:
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
        ".webp": lambda h: h[:4] == b"RIFF" and h[8:12] == b"WEBP",
        ".bmp": lambda h: h[:2] == b"BM",
        ".tif": lambda h: h[:4] in (b"II*\x00", b"MM\x00*"),
        ".tiff": lambda h: h[:4] in (b"II*\x00", b"MM\x00*"),
        ".svg": _is_svg,
        ".mp3": _is_mp3,
        ".m4a": lambda h: h[4:8] == b"ftyp",
        # ADTS frame sync (raw AAC), or an ID3 tag in front of it.
        ".aac": lambda h: h[:3] == b"ID3" or (h[0] == 0xFF and h[1] & 0xF6 == 0xF0),
        ".wav": lambda h: h[:4] == b"RIFF" and h[8:12] == b"WAVE",
        ".flac": lambda h: h[:4] == b"fLaC" or h[:3] == b"ID3",
        ".ogg": lambda h: h[:4] == b"OggS",
        ".opus": lambda h: h[:4] == b"OggS",
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
            kind = kind_of(e.name)
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

    def playable(self, names: list[str], kinds: tuple[str, ...]) -> list[str]:
        """The names that exist in the library and are of one of ``kinds``, in order."""
        return [n for n in names if kind_of(n) in kinds and self.exists(n)]

    def new_incoming_file(self) -> BinaryIO:
        """Open a temp file for an upload in progress (on the media filesystem)."""
        self.incoming.mkdir(parents=True, exist_ok=True)
        path = self.incoming / (secrets.token_hex(8) + ".part")
        return open(path, "w+b")  # noqa: SIM115 - caller owns the handle

    def add(self, tmp_path: Path, raw_name: str) -> str:
        """Move a finished upload into the library. Returns the final filename.

        Always removes ``tmp_path`` if the file is rejected.
        """
        tmp_path = Path(tmp_path)
        try:
            name = safe_name(raw_name)
            ext = os.path.splitext(name)[1]
            if ext == ".gifv":
                ext = gifv_real_ext(tmp_path)
                name = name[: -len(".gifv")] + ext
            if os.path.getsize(tmp_path) == 0:
                raise MediaError("uploaded file is empty")
            if not sniff_ok(tmp_path, ext):
                raise MediaError(
                    f"file content does not look like a valid {ext} file", 415
                )
            if ext == ".webp" and _animated_webp_file(tmp_path):
                raise MediaError(
                    "animated WebP can't be played on the Pi; convert it to MP4 or GIF "
                    "first (see README, \"NFTs\")", 415
                )
            if ext == ".svg":
                png = tmp_path.with_name(tmp_path.name + ".png")
                try:
                    rasterize_svg(tmp_path, png)
                finally:
                    _unlink_quiet(tmp_path)
                tmp_path = png
                name = name[: -len(ext)] + ".png"
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


def _animated_webp_file(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            return is_animated_webp(f.read(32))
    except OSError:
        return False


def _unlink_quiet(path: Path) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass
