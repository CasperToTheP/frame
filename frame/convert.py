"""Animated WebP → MP4, at upload.

No ffmpeg before 8.0 can decode animated WebP, so mpv on Raspberry Pi OS can't
play it. Pillow can: it decodes the frames one at a time here, and the ffmpeg
command line encodes them to H.264, which the Pi decodes in hardware. This is
the one format Frame converts; everything else plays as uploaded.

Frames are written at a constant 30 fps, each repeated for as long as the
WebP shows it, so per-frame timing is kept. Transparency becomes black, and
anything larger than 1920×1080 is scaled down.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

from .media import MediaError
from .thumbs import lower_priority

log = logging.getLogger("frame.convert")

FPS = 30
MAX_SIZE = (1920, 1080)
TIMEOUT = 15 * 60


def fit_even(width: int, height: int, box: tuple[int, int] = MAX_SIZE) -> tuple[int, int]:
    """Scale (width, height) down to fit ``box``, keeping the aspect ratio, with
    even sides (H.264 in yuv420p needs them)."""
    scale = min(1.0, box[0] / width, box[1] / height)
    w, h = int(width * scale), int(height * scale)
    return max(2, w - w % 2), max(2, h - h % 2)


def animated_webp_to_mp4(src: Path, dst: Path, ffmpeg_bin: str = "ffmpeg") -> None:
    """Convert, or raise MediaError with a message for the user."""
    try:
        from PIL import Image  # imported only when needed: it's not small
    except ImportError as exc:
        raise MediaError("converting animated WebP needs Pillow; re-run the installer", 415) \
            from exc
    ffmpeg = shutil.which(ffmpeg_bin)
    if not ffmpeg:
        raise MediaError("converting animated WebP needs ffmpeg (sudo apt install ffmpeg)", 415)

    try:
        _convert(Image, src, dst, ffmpeg)
    except MediaError:
        raise
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError) as exc:
        # Pillow's errors for broken or unsupported files.
        raise MediaError(f"could not read this animated WebP ({exc})", 415) from exc


def _convert(Image, src: Path, dst: Path, ffmpeg: str) -> None:  # noqa: N803 - the module
    with Image.open(src) as im:
        frames = getattr(im, "n_frames", 1)
        w, h = fit_even(*im.size)
        cmd = [
            ffmpeg, "-v", "error", "-y",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-r", str(FPS), "-i", "-",
            # Modest settings: this runs on the Pi next to the playing artwork.
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-threads", "2",
            "-x264-params", "rc-lookahead=10",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-an", str(dst),
        ]
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, preexec_fn=lower_priority)
        written = 0
        carry = 0.0
        try:
            last = b""
            for i in range(frames):
                im.seek(i)
                frame = im.convert("RGBA")
                black = Image.new("RGBA", frame.size, (0, 0, 0, 255))
                frame = Image.alpha_composite(black, frame).convert("RGB")
                if frame.size != (w, h):
                    frame = frame.resize((w, h), Image.LANCZOS)
                last = frame.tobytes()
                # Show each frame for its own duration (default 100 ms).
                carry += max(im.info.get("duration") or 100, 10) / 1000 * FPS
                repeats, carry = int(carry), carry - int(carry)
                for _ in range(repeats):
                    proc.stdin.write(last)
                    written += 1
            if written == 0 and last:
                proc.stdin.write(last)
            proc.stdin.close()
            code = proc.wait(TIMEOUT)
        except BrokenPipeError:
            code = proc.wait(TIMEOUT)
        except BaseException:
            proc.kill()
            proc.wait()
            raise
        if code != 0:
            lines = proc.stderr.read().decode("utf-8", "replace").strip().splitlines()
            raise MediaError("could not convert the animated WebP"
                             + (f": {lines[-1]}" if lines else ""), 422)
        proc.stderr.close()
    log.info("converted animated WebP (%d frames, %dx%d) to MP4", frames, w, h)
