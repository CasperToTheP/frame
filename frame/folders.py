"""Folders in the library: a way to organise files, kept in library.json.

Folders are labels, not directories: every file stays in the flat media
directory under its unique name, and library.json only records which folder
it's filed under. So moving a file never changes its name, and playlists,
thumbnails and the player keep working. Folders can be nested ("Japan/Night").

A playlist can hold a whole folder as ``"folder:<path>"``. It's expanded to
the files in that folder and its subfolders (by name) whenever the playlist is
played, and again when the folder's contents change, so the playlist follows
the folder. Filenames can't contain ":" (media.safe_name), so this can't clash.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

from .media import MediaError

log = logging.getLogger("frame.folders")

FOLDER_PREFIX = "folder:"
MAX_FOLDERS = 200
MAX_NAME = 40
MAX_DEPTH = 5


def is_folder_ref(item: str) -> bool:
    return item.startswith(FOLDER_PREFIX)


def folder_ref(path: str) -> str:
    return FOLDER_PREFIX + path


def ref_path(item: str) -> str:
    return item[len(FOLDER_PREFIX):]


def clean_name(name: Any) -> str:
    """One folder name (not a path), or MediaError."""
    if not isinstance(name, str):
        raise MediaError("folder name must be text")
    name = " ".join(name.replace("/", " ").split())
    if not name or len(name) > MAX_NAME or name.startswith("."):
        raise MediaError(f"folder name must be 1-{MAX_NAME} characters")
    return name


def clean_path(path: Any) -> str:
    """A folder path like "Japan/Night" ("" is the top of the library), or MediaError."""
    if path in (None, ""):
        return ""
    if not isinstance(path, str):
        raise MediaError("folder must be text")
    parts = [clean_name(p) for p in path.split("/") if p.strip()]
    if len(parts) > MAX_DEPTH:
        raise MediaError(f"folders can be at most {MAX_DEPTH} levels deep")
    return "/".join(parts)


def parent_of(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else ""


def is_within(path: str, folder: str) -> bool:
    """True if ``path`` is ``folder`` or inside it ("" contains everything)."""
    return folder == "" or path == folder or path.startswith(folder + "/")


class FolderStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    # --- reading ------------------------------------------------------------------

    def load(self) -> dict[str, Any]:
        """{"folders": [paths], "files": {filename: folder}}. Never raises."""
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raw = {}
        except (OSError, ValueError) as exc:
            log.warning("folder file %s unreadable (%s); starting empty", self.path, exc)
            raw = {}
        folders: set[str] = set()
        for p in raw.get("folders", []) if isinstance(raw, dict) else []:
            try:
                folders.add(clean_path(p))
            except MediaError:
                continue
        files = {}
        for name, folder in (raw.get("files", {}) if isinstance(raw, dict) else {}).items():
            try:
                folder = clean_path(folder)
            except MediaError:
                continue
            if isinstance(name, str) and folder:
                files[name] = folder
                folders.add(folder)
        # Every parent of a folder is a folder too.
        for p in list(folders):
            while "/" in p:
                p = parent_of(p)
                folders.add(p)
        folders.discard("")
        return {"folders": sorted(folders, key=str.lower), "files": files}

    def folder_of(self, name: str, data: dict[str, Any] | None = None) -> str:
        return (data or self.load())["files"].get(name, "")

    # --- writing ------------------------------------------------------------------

    def _save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"folders": data["folders"], "files": data["files"]}, f,
                      indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    def create(self, parent: Any, name: Any) -> str:
        with self._lock:
            data = self.load()
            parent = clean_path(parent)
            if parent and parent not in data["folders"]:
                raise MediaError(f"no folder called {parent!r}", 404)
            path = f"{parent}/{clean_name(name)}" if parent else clean_name(name)
            clean_path(path)  # depth check
            if path in data["folders"]:
                raise MediaError(f"there's already a folder called {path!r}", 409)
            if len(data["folders"]) >= MAX_FOLDERS:
                raise MediaError(f"at most {MAX_FOLDERS} folders", 400)
            data["folders"].append(path)
            self._save(data)
        log.info("created folder %r", path)
        return path

    def rename(self, path: Any, new_name: Any) -> tuple[str, str]:
        """Rename a folder (its subfolders and files come along). Returns (old, new)."""
        old = clean_path(path)
        parent = parent_of(old)
        new = f"{parent}/{clean_name(new_name)}" if parent else clean_name(new_name)
        return self._relocate(old, new, "renamed")

    def move_folder(self, path: Any, parent: Any) -> tuple[str, str]:
        """Move a folder (with what's in it) into ``parent`` ("" = top). Returns (old, new)."""
        old, parent = clean_path(path), clean_path(parent)
        if old and is_within(parent, old):
            raise MediaError("a folder can't go inside itself", 400)
        name = old.rsplit("/", 1)[-1]
        return self._relocate(old, f"{parent}/{name}" if parent else name, "moved")

    def _relocate(self, old: str, new: str, verb: str) -> tuple[str, str]:
        with self._lock:
            data = self.load()
            if old not in data["folders"]:
                raise MediaError(f"no folder called {old!r}", 404)
            parent = parent_of(new)
            if parent and parent not in data["folders"]:
                raise MediaError(f"no folder called {parent!r}", 404)
            if new != old and new in data["folders"]:
                raise MediaError(f"there's already a folder called {new!r}", 409)
            # Moving must not push subfolders past the depth limit.
            depth = max(p.count("/") for p in data["folders"] if is_within(p, old))
            clean_path("/".join(["x"] * (depth - old.count("/") + new.count("/") + 1)))
            data["folders"] = [_moved(p, old, new) for p in data["folders"]]
            data["files"] = {n: _moved(f, old, new) for n, f in data["files"].items()}
            self._save(data)
        log.info("%s folder %r to %r", verb, old, new)
        return old, new

    def delete(self, path: Any) -> str:
        """Remove a folder; what was in it moves up to its parent. Returns the parent."""
        with self._lock:
            data = self.load()
            path = clean_path(path)
            if path not in data["folders"]:
                raise MediaError(f"no folder called {path!r}", 404)
            parent = parent_of(path)
            data["folders"] = [_moved(p, path, parent) for p in data["folders"] if p != path]
            data["files"] = {n: _moved(f, path, parent) for n, f in data["files"].items()}
            data["files"] = {n: f for n, f in data["files"].items() if f}
            self._save(data)
        log.info("deleted folder %r (contents moved to %r)", path, parent or "top")
        return parent

    def move(self, names: list[str], folder: Any) -> str:
        with self._lock:
            data = self.load()
            folder = clean_path(folder)
            if folder and folder not in data["folders"]:
                raise MediaError(f"no folder called {folder!r}", 404)
            for name in names:
                if folder:
                    data["files"][name] = folder
                else:
                    data["files"].pop(name, None)
            self._save(data)
        return folder

    def forget(self, name: str) -> None:
        """A file was deleted."""
        with self._lock:
            data = self.load()
            if data["files"].pop(name, None) is not None:
                self._save(data)

    # --- playlists ----------------------------------------------------------------

    def expand(self, items: list[str], all_names: list[str]) -> list[str]:
        """Playlist items with each "folder:<path>" replaced by the files filed in that
        folder or below it (by name), without duplicates. Plain filenames pass through."""
        if not any(is_folder_ref(i) for i in items):
            return list(items)
        data = self.load()
        out: list[str] = []
        for item in items:
            if is_folder_ref(item):
                folder = ref_path(item)
                out.extend(n for n in sorted(all_names, key=str.lower)
                           if is_within(data["files"].get(n, ""), folder) and folder)
            else:
                out.append(item)
        return list(dict.fromkeys(out))


def _moved(path: str, old: str, new: str) -> str:
    """``path`` after folder ``old`` became ``new``."""
    if path == old:
        return new
    if path.startswith(old + "/"):
        rest = path[len(old) + 1:]
        return f"{new}/{rest}" if new else rest
    return path
