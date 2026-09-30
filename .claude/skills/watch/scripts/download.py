#!/usr/bin/env python3
"""Download a video via yt-dlp, or resolve a local file path.

Also fetches subtitles (manual first, then auto-generated) in VTT format so
transcribe.py can parse them without needing Whisper.
"""
from __future__ import annotations

import base64
import binascii
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse


VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi", ".flv", ".wmv"}

# Private copy of cookies decoded from WATCH_COOKIES_B64.
COOKIES_COPY = Path.home() / ".config" / "watch" / "cookies.txt"


def _setting(name: str) -> str | None:
    """Read a setting from the environment, then ~/.config/watch/.env."""
    from config import read_env_file

    value = os.environ.get(name) or read_env_file().get(name)
    return value.strip() if value and value.strip() else None


def cookie_args() -> list[str]:
    """yt-dlp cookie flags from the user's settings, or [] if none are set.

    Sites like YouTube may demand sign-in ("confirm you're not a bot") from
    cloud or datacenter IPs; the user's own browser cookies get past that.
    Checked in order:
      WATCH_COOKIES_B64          base64 of a Netscape cookies.txt — fits a
                                 single-line secret (cloud environments)
      WATCH_COOKIES_FILE         path to a Netscape cookies.txt
      WATCH_COOKIES_FROM_BROWSER browser to read cookies from, e.g. "chrome"
    Cookie values are never printed.
    """
    encoded = _setting("WATCH_COOKIES_B64")
    if encoded:
        try:
            data = base64.b64decode("".join(encoded.split()), validate=True)
        except (binascii.Error, ValueError):
            print("[watch] WATCH_COOKIES_B64 is not valid base64 — ignoring it", file=sys.stderr)
        else:
            COOKIES_COPY.parent.mkdir(parents=True, exist_ok=True)
            # Create owner-only before writing so the cookies are never world-readable.
            fd = os.open(COOKIES_COPY, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.chmod(COOKIES_COPY, 0o600)
            print("[watch] using cookies from WATCH_COOKIES_B64", file=sys.stderr)
            return ["--cookies", str(COOKIES_COPY)]

    path = _setting("WATCH_COOKIES_FILE")
    if path:
        cookie_file = Path(path).expanduser()
        if cookie_file.is_file():
            print(f"[watch] using cookies from {cookie_file}", file=sys.stderr)
            return ["--cookies", str(cookie_file)]
        print(f"[watch] WATCH_COOKIES_FILE not found: {cookie_file} — ignoring it", file=sys.stderr)

    browser = _setting("WATCH_COOKIES_FROM_BROWSER")
    if browser:
        print(f"[watch] using cookies from browser: {browser}", file=sys.stderr)
        return ["--cookies-from-browser", browser]

    return []


def is_url(source: str) -> bool:
    if source.startswith("-"):
        return False
    parsed = urlparse(source)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def resolve_local(path: str) -> dict:
    p = Path(path).expanduser().resolve()
    if not p.exists():
        raise SystemExit(f"File not found: {p}")
    if p.suffix.lower() not in VIDEO_EXTS:
        print(
            f"[watch] warning: {p.suffix} is not a known video extension, proceeding anyway",
            file=sys.stderr,
        )
    return {
        "video_path": str(p),
        "subtitle_path": None,
        "info": {"title": p.name, "url": str(p)},
        "downloaded": False,
    }


def _pick_subtitle(out_dir: Path) -> Path | None:
    candidates = sorted(out_dir.glob("video*.vtt"))
    if not candidates:
        return None
    preferred = [
        c for c in candidates
        if any(marker in c.name for marker in (".en.", ".en-US.", ".en-GB.", ".en-orig."))
    ]
    return preferred[0] if preferred else candidates[0]


def _pick_video(out_dir: Path) -> Path | None:
    for ext in (".mp4", ".mkv", ".webm", ".mov", ".m4a", ".mp3", ".opus"):
        for candidate in out_dir.glob(f"video*{ext}"):
            return candidate
    for candidate in out_dir.glob("video.*"):
        if candidate.suffix.lower() in VIDEO_EXTS:
            return candidate
    return None


def fetch_captions(url: str, out_dir: Path) -> dict:
    """Fetch metadata and best available VTT captions without downloading video."""
    if shutil.which("yt-dlp") is None:
        raise SystemExit("yt-dlp is not installed. Install with: brew install yt-dlp")

    out_dir.mkdir(parents=True, exist_ok=True)
    output_template = str(out_dir / "video.%(ext)s")
    cmd = [
        "yt-dlp",
        "--skip-download",
        *cookie_args(),
        "--write-info-json",
        "--write-subs",
        "--write-auto-subs",
        "--sub-langs", "en.*",
        "--sub-format", "vtt",
        "--convert-subs", "vtt",
        "--no-playlist",
        "--ignore-errors",
        "-o", output_template,
        "--",
        url,
    ]
    subprocess.run(cmd, stdout=sys.stderr, stderr=sys.stderr)
    subtitle = _pick_subtitle(out_dir)
    info = _read_info(out_dir / "video.info.json", url)
    return {
        "video_path": None,
        "subtitle_path": str(subtitle) if subtitle else None,
        "info": info or {"url": url},
        "downloaded": False,
    }


def _read_info(info_path: Path, url: str) -> dict:
    info: dict = {}
    if info_path.exists():
        try:
            raw = json.loads(info_path.read_text(encoding="utf-8"))
            info = {
                "title": raw.get("title"),
                "uploader": raw.get("uploader") or raw.get("channel"),
                "duration": raw.get("duration"),
                "url": raw.get("webpage_url") or url,
            }
        except Exception as exc:
            print(f"[watch] info.json parse failed: {exc}", file=sys.stderr)
            info = {"url": url}
    return info


def download_url(
    url: str,
    out_dir: Path,
    audio_only: bool = False,
) -> dict:
    if shutil.which("yt-dlp") is None:
        raise SystemExit("yt-dlp is not installed. Install with: brew install yt-dlp")

    out_dir.mkdir(parents=True, exist_ok=True)
    output_template = str(out_dir / "video.%(ext)s")

    fmt = "ba/bestaudio" if audio_only else "bv*[height<=720]+ba/b[height<=720]/bv+ba/b"
    cmd = [
        "yt-dlp",
        "-N", "8",
        "-f", fmt,
        "--merge-output-format", "mp4",
        *cookie_args(),
        "--write-info-json",
        "--write-subs",
        "--write-auto-subs",
        "--sub-langs", "en.*",
        "--sub-format", "vtt",
        "--convert-subs", "vtt",
        "--no-playlist",
        "--ignore-errors",
        "-o", output_template,
        "--",
        url,
    ]

    # yt-dlp may exit non-zero if a subtitle variant fails (e.g. 429) even when
    # the video itself downloaded fine. Treat "video file present" as success.
    result = subprocess.run(cmd, stdout=sys.stderr, stderr=sys.stderr)
    video = _pick_video(out_dir)
    if video is None:
        hint = ""
        if "--cookies" not in cmd and "--cookies-from-browser" not in cmd:
            hint = (
                ". If the site asked to sign in or 'confirm you're not a bot': for YouTube, "
                "use the vidIQ tools instead (see SKILL.md, 'vidIQ fallback for YouTube'), or "
                "set WATCH_COOKIES_B64, WATCH_COOKIES_FILE or WATCH_COOKIES_FROM_BROWSER "
                "(see SKILL.md, 'Cookies for YouTube')"
            )
        raise SystemExit(
            f"yt-dlp did not produce a video file in {out_dir} (exit {result.returncode}){hint}"
        )

    subtitle = _pick_subtitle(out_dir)
    info = _read_info(out_dir / "video.info.json", url)

    return {
        "video_path": str(video),
        "subtitle_path": str(subtitle) if subtitle else None,
        "info": info or {"url": url},
        "downloaded": True,
    }


def download(
    source: str,
    out_dir: Path,
    audio_only: bool = False,
) -> dict:
    if is_url(source):
        return download_url(source, out_dir, audio_only=audio_only)
    return resolve_local(source)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("usage: download.py <url-or-path> <out-dir>", file=sys.stderr)
        raise SystemExit(2)
    result = download(sys.argv[1], Path(sys.argv[2]))
    print(json.dumps(result, indent=2))
