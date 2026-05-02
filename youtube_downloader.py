"""youtube_downloader.py — YouTube video & thumbnail downloader.

Architecture
────────────
This module is the ONLY place where YouTube logic lives.
TikTok, Facebook, admin, cooldown, quota, referral, and broadcast
systems are completely untouched.

Supported link types
────────────────────
  • youtube.com/watch?v=…
  • youtube.com/shorts/…
  • youtu.be/…
  • m.youtube.com/…

Quality modes
─────────────
FREE  — selected height, pre-merged mp4.  Sent as send_video.
VIP   — selected height, best quality merge via FFmpeg.  Sent as document.

Public API
──────────
  is_youtube_url(text)                       → bool
  extract_youtube_url(text)                  → str | None
  fetch_youtube_info(url)                    → YTVideoInfo  (async)
  download_youtube_video(url, uid, msg_id,
                         height)             → YTDownloadResult  (async)
  download_thumbnail_from_url(thumb_url,
                              uid, msg_id)   → str | None  (async)
  YTVideoInfo, YTDownloadResult              — structured containers
  MAX_TG_SIZE_MB                             — Telegram limit constant
"""

import asyncio
import glob as _glob
import os
import re
import shutil
from dataclasses import dataclass, field
from typing import Optional

import aiohttp
import yt_dlp

from downloader import TEMP_DIR
from logger import log

MAX_TG_SIZE_MB = 50

_FFMPEG = shutil.which("ffmpeg") or ""

YOUTUBE_PATTERN = re.compile(
    r"https?://(?:www\.|m\.)?(?:youtube\.com/(?:watch\?(?:[^&\s]*&)*v=|shorts/|embed/|v/)|youtu\.be/)[^\s]+"
)


# ─── Result containers ────────────────────────────────────────────────────────

@dataclass
class YTFormat:
    height: int
    label: str
    ext: str


@dataclass
class YTVideoInfo:
    ok: bool
    title: str = ""
    thumbnail: str = ""
    duration: int = 0
    formats: list = field(default_factory=list)
    webpage_url: str = ""
    error_msg: str = ""


@dataclass
class YTDownloadResult:
    ok: bool
    filepath: Optional[str] = None
    title: str = ""
    size_mb: float = 0.0
    format_note: str = ""
    resolution: str = ""
    error_type: str = ""
    user_msg: str = ""


# ─── URL helpers ──────────────────────────────────────────────────────────────

def is_youtube_url(text: str) -> bool:
    return bool(YOUTUBE_PATTERN.search(text))


def extract_youtube_url(text: str) -> Optional[str]:
    m = YOUTUBE_PATTERN.search(text)
    return m.group(0) if m else None


# ─── yt-dlp option builders ───────────────────────────────────────────────────

def _ydl_opts_info() -> dict:
    return {
        "quiet":         True,
        "no_warnings":   True,
        "skip_download": True,
        "noplaylist":    True,
    }


def _ydl_opts_download(format_str: str, outtmpl: str) -> dict:
    opts: dict = {
        "quiet":                True,
        "no_warnings":          True,
        "noplaylist":           True,
        "outtmpl":              outtmpl,
        "format":               format_str,
        "merge_output_format":  "mp4",
    }
    if _FFMPEG:
        opts["ffmpeg_location"] = _FFMPEG
    return opts


# ─── Info fetch (sync, runs in executor) ─────────────────────────────────────

def _fetch_info_sync(url: str) -> YTVideoInfo:
    try:
        with yt_dlp.YoutubeDL(_ydl_opts_info()) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        log.error(f"[YT] info fetch error for {url}: {e}")
        return YTVideoInfo(ok=False, error_msg=str(e))

    if not info:
        return YTVideoInfo(ok=False, error_msg="No info returned")

    seen_heights: set = set()
    formats: list = []

    for f in (info.get("formats") or []):
        height = f.get("height")
        if not height or f.get("vcodec", "none") == "none":
            continue
        if height in seen_heights:
            continue
        seen_heights.add(height)
        formats.append(YTFormat(
            height=height,
            label=f"{height}p",
            ext=f.get("ext", "mp4"),
        ))

    formats.sort(key=lambda x: x.height, reverse=True)

    return YTVideoInfo(
        ok=True,
        title=info.get("title", "YouTube Video"),
        thumbnail=info.get("thumbnail", ""),
        duration=int(info.get("duration") or 0),
        formats=formats,
        webpage_url=info.get("webpage_url", url),
    )


async def fetch_youtube_info(url: str) -> YTVideoInfo:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _fetch_info_sync, url)


# ─── Video download (sync, runs in executor) ──────────────────────────────────

def _build_format(height: int) -> str:
    """Build yt-dlp format string for the requested height."""
    if height == 0:
        return "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
    return (
        f"bestvideo[height<={height}][ext=mp4]+bestaudio[ext=m4a]"
        f"/best[height<={height}][ext=mp4]"
        f"/bestvideo[height<={height}]+bestaudio"
        f"/best[height<={height}]"
    )


def _download_sync(url: str, height: int, tpl: str) -> tuple:
    """
    Returns (filepath, size_mb, format_note, error_msg).
    filepath is None on failure.
    """
    fmt = _build_format(height)
    note = f"{height}p" if height else "Best"
    opts = _ydl_opts_download(fmt, tpl)

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            candidate = ydl.prepare_filename(info)
    except Exception as e:
        return None, 0.0, note, str(e)

    base = os.path.splitext(candidate)[0]
    final_path: Optional[str] = None

    for ext in (".mp4", ".mkv", ".webm"):
        p = base + ext
        if os.path.exists(p):
            final_path = p
            break

    if not final_path:
        matches = _glob.glob(base + ".*")
        if matches:
            final_path = matches[0]

    if not final_path or not os.path.exists(final_path):
        return None, 0.0, note, "Output file not found after download"

    size_mb = os.path.getsize(final_path) / (1024 * 1024)
    log.info(f"[YT] download complete: {final_path} ({size_mb:.2f} MB)")
    return final_path, size_mb, note, ""


async def download_youtube_video(
    url: str,
    uid: int,
    msg_id: int,
    height: int = 0,
) -> YTDownloadResult:
    tpl = os.path.join(TEMP_DIR, f"yt_{uid}_{msg_id}.%(ext)s")
    loop = asyncio.get_event_loop()
    filepath, size_mb, note, err = await loop.run_in_executor(
        None, _download_sync, url, height, tpl
    )

    if err or not filepath:
        log.error(f"[YT] download_youtube_video failed for user={uid}: {err}")
        return YTDownloadResult(
            ok=False,
            error_type="download_failed",
            user_msg=(
                "❌ <b>YouTube video ဒေါင်းမရပါ</b>\n\n"
                "Link မှားနေသည် / Region-blocked / Copyright ကန့်သတ်ချက် ဖြစ်နိုင်သည်"
            ),
        )

    return YTDownloadResult(
        ok=True,
        filepath=filepath,
        size_mb=size_mb,
        format_note=note,
        resolution=note,
    )


# ─── Thumbnail download ───────────────────────────────────────────────────────

async def download_thumbnail_from_url(
    thumb_url: str,
    uid: int,
    msg_id: int,
) -> Optional[str]:
    """Download a thumbnail from a direct URL. Returns local filepath or None."""
    filepath = os.path.join(TEMP_DIR, f"yt_thumb_{uid}_{msg_id}.jpg")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                thumb_url,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status != 200:
                    log.error(f"[YT] thumbnail HTTP {resp.status} for user {uid}")
                    return None
                with open(filepath, "wb") as f:
                    async for chunk in resp.content.iter_chunked(65536):
                        f.write(chunk)
        log.info(f"[YT] thumbnail downloaded for user {uid}: {filepath}")
        return filepath
    except Exception as e:
        log.error(f"[YT] thumbnail download error for user {uid}: {e}")
        return None
