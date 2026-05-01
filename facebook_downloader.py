"""facebook_downloader.py — Dedicated Facebook video provider module.

Architecture
────────────
This module is the ONLY place where Facebook logic lives.
TikTok, admin, cooldown, quota, referral, and broadcast systems are
completely untouched.

Supported link types
────────────────────
  • facebook.com/watch/?v=…
  • facebook.com/<user>/videos/<id>
  • facebook.com/reel/<id>
  • fb.watch/<code>
  • facebook.com/share/v/<token>
  • facebook.com/share/r/<token>

Quality modes
─────────────
FREE — `best[ext=mp4]` format family.  Single pre-merged MP4 stream.
        No FFmpeg required.  Safe for Telegram send_video (max 720p).

VIP  — `bestvideo+bestaudio` format family.  Highest available resolution.
        FFmpeg merges streams into an MP4 container WITHOUT re-encoding
        (remux only).  Delivered as send_document to bypass Telegram
        compression.

Provider pipeline (yt-dlp is always PRIMARY)
─────────────────────────────────────────────
Step 1  yt-dlp on the original URL.
Step 2  If yt-dlp hit a login redirect, parse ?next= and retry.
Step 3  For /share/ links — urllib Location-header fallback + retry.

Error classification happens ONLY after the provider has failed.
No URL is ever pre-emptively rejected.
All blocking I/O runs in a thread executor.

Public API
──────────
  is_facebook_url(text)                         → bool
  extract_facebook_url(text)                    → str | None
  download_facebook_video(url, uid, msg_id,
                          vip_mode=False)        → DownloadResult  (async)
  DownloadResult                                — structured result container
  MAX_TG_SIZE_MB                                — Telegram limit constant
"""

import asyncio
import glob as _glob
import math
import os
import re
import shutil
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

import yt_dlp
import yt_dlp.utils as _ydl_utils

from downloader import TEMP_DIR
from logger import log

# ─── Constants ────────────────────────────────────────────────────────────────

MAX_TG_SIZE_MB = 50

# FFmpeg binary — needed for VIP bestvideo+bestaudio merge (remux, no re-encode)
_FFMPEG = shutil.which("ffmpeg") or ""

# FREE format: best available pre-merged MP4 at ORIGINAL dimensions.
# height<=720 is intentionally REMOVED — we do not resize or restrict dimensions.
# The video's original aspect ratio (9:16, 16:9, 1:1, etc.) is preserved as-is.
_YDL_FORMAT_FREE = (
    "best[ext=mp4]"
    "/best"
)

# VIP format: highest resolution + best audio, merged by FFmpeg into MP4 container
_YDL_FORMAT_VIP = (
    "bestvideo[ext=mp4]+bestaudio[ext=m4a]"
    "/bestvideo[ext=mp4]+bestaudio"
    "/bestvideo+bestaudio"
    "/best"
)

# ─── Link detection ───────────────────────────────────────────────────────────

FACEBOOK_PATTERN = re.compile(
    r'https?://(?:www\.|m\.|web\.)?(?:facebook\.com|fb\.watch)/\S+',
    re.IGNORECASE,
)

_SHARE_PATH_RE = re.compile(r'/share/', re.IGNORECASE)

_LOGIN_URL_RE = re.compile(
    r'https?://(?:[\w-]+\.)?facebook\.com/login/[^\s"\']*',
    re.IGNORECASE,
)

# ─── Structured result ────────────────────────────────────────────────────────

@dataclass
class DownloadResult:
    """Structured provider result — always returned, never raises to the caller.

    Fields (success)
    ────────────────
    ok          True
    filepath    Absolute path to downloaded file.
    title       Video title from yt-dlp.
    size_mb     File size in MB.
    format_note Human-readable format string logged + shown (e.g. "1080p").
    vip_mode    Whether VIP quality was used for this download.

    Fields (failure)
    ────────────────
    ok          False
    error_type  "auth_required" | "private" | "unavailable" |
                "unsupported" | "too_large" | "download_failed"
    user_msg    Ready-to-send Myanmar message.
    raw_error   Raw yt-dlp error string (for logging only).
    """
    ok:          bool
    filepath:    str   | None = None
    title:       str   | None = None
    size_mb:     float | None = None
    format_note: str   | None = None
    width:       int   | None = None   # original pixel width  (never altered)
    height:      int   | None = None   # original pixel height (never altered)
    aspect_ratio: str  | None = None   # simplified ratio string, e.g. "16:9", "9:16", "1:1"
    vip_mode:    bool         = False
    error_type:  str   | None = None
    user_msg:    str   | None = None
    raw_error:   str   | None = None


# ─── User-facing messages ─────────────────────────────────────────────────────

_USER_MESSAGES: dict[str, str] = {
    "private": (
        "🔒 ဤ Facebook ဗီဒီယိုသည် Private / Friends-only ဖြစ်သဖြင့် ဒေါင်းမရပါ။"
    ),
    "auth_required": (
        "❌ Facebook video ဒေါင်းမရပါ\n\n"
        "ဤ video ကြည့်ရှုရန် Facebook Account လိုအပ်နေသည်။\n"
        "Bot မှ Public videos (Account မလိုသော) သာ ပံ့ပိုးနိုင်ပါသည်။"
    ),
    "unavailable": (
        "❌ Facebook video မရနိုင်ပါ "
        "(ဖျက်သိမ်းပြီး သို့မဟုတ် မတည်ရှိတော့ပါ)။"
    ),
    "unsupported": (
        "❌ Facebook video link ကို ဒေါင်းမရပါ\n\n"
        "Public video, reel, watch link သာ ပံ့ပိုးသည်။\n"
        "Share link ဖြစ်ပါက Public ဖြစ်ရမည်။"
    ),
    "too_large": (
        "⚠️ ဖိုင်ကြီးနေသဖြင့် Telegram သို့ တိုက်ရိုက်ပို့မရပါ (>50 MB)။"
    ),
    "download_failed": (
        "⚠️ Facebook video ဒေါင်းမရပါ။ နောက်မှ ထပ်ကြိုးစားပါ။"
    ),
}


def _user_msg(error_type: str) -> str:
    return _USER_MESSAGES.get(error_type, _USER_MESSAGES["download_failed"])


def _fail(error_type: str, raw: str = "") -> DownloadResult:
    log.info(f"[FB] result=FAIL type={error_type}")
    return DownloadResult(
        ok=False,
        error_type=error_type,
        user_msg=_user_msg(error_type),
        raw_error=raw,
    )


# ─── URL helpers ──────────────────────────────────────────────────────────────

def is_facebook_url(text: str) -> bool:
    return bool(FACEBOOK_PATTERN.search(text))


def extract_facebook_url(text: str) -> str | None:
    m = FACEBOOK_PATTERN.search(text)
    return m.group(0) if m else None


# ─── Error classifier ─────────────────────────────────────────────────────────

_AUTH_KEYWORDS = (
    "registered users",
    "only available for",
    "login required",
    "must be logged",
    "authentication required",
    "please log in",
    "use --cookies",
    "cookies for the auth",
)
_PRIVATE_KEYWORDS = (
    "private video",
    "friends only",
    "only me",
)
_UNAVAILABLE_KEYWORDS = (
    "does not exist",
    "removed",
    "no video found",
    "no longer available",
    "has been deleted",
    "content not found",
)
_UNSUPPORTED_KEYWORDS = (
    "no suitable format",
    "unsupported url",
    "is not a valid url",
    "no video formats",
)


def _classify(raw_error: str) -> str:
    """Map a raw yt-dlp error string to an error_type.
    Called ONLY after the provider has failed — no pre-emptive rejection.
    """
    err = raw_error.lower()
    if any(k in err for k in _AUTH_KEYWORDS):
        log.info("[FB] classified as auth_required")
        return "auth_required"
    if any(k in err for k in _PRIVATE_KEYWORDS):
        log.info("[FB] classified as private")
        return "private"
    if any(k in err for k in _UNAVAILABLE_KEYWORDS):
        log.info("[FB] classified as unavailable")
        return "unavailable"
    if any(k in err for k in _UNSUPPORTED_KEYWORDS):
        log.info("[FB] classified as unsupported")
        return "unsupported"
    log.info("[FB] classified as download_failed")
    return "download_failed"


# ─── Pipeline helpers ─────────────────────────────────────────────────────────

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
_YDL_HEADERS = {"User-Agent": _UA}


class _StopRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _urllib_first_redirect(url: str) -> str | None:
    try:
        opener = urllib.request.build_opener(_StopRedirect())
        req    = urllib.request.Request(url, headers={"User-Agent": _UA})
        opener.open(req, timeout=15)
    except urllib.error.HTTPError as exc:
        if exc.code in (301, 302, 303, 307, 308):
            loc = exc.headers.get("Location", "").strip()
            if loc:
                return loc
    except Exception:
        pass
    return None


def _story_url_from_login(login_url: str) -> str | None:
    try:
        parsed   = urllib.parse.urlparse(login_url)
        qs       = urllib.parse.parse_qs(parsed.query)
        next_raw = qs.get("next", [None])[0]
        if not next_raw:
            return None
        p2         = urllib.parse.urlparse(next_raw)
        qs2        = urllib.parse.parse_qs(p2.query)
        story_fbid = qs2.get("story_fbid", [None])[0]
        page_id    = qs2.get("id",         [None])[0]
        if story_fbid and page_id:
            return (
                f"https://www.facebook.com/story.php"
                f"?story_fbid={story_fbid}&id={page_id}"
            )
        if "facebook.com" in next_raw and "login" not in next_raw:
            return next_raw
    except Exception as exc:
        log.warning(f"[FB] story URL extraction failed: {exc}")
    return None


def _aspect_ratio_str(width: int | None, height: int | None) -> str | None:
    """Return a simplified aspect ratio string, e.g. '16:9', '9:16', '1:1'.
    Returns None if either dimension is unknown or zero.
    No resampling, cropping, or padding is ever performed — this is read-only.
    """
    if not width or not height:
        return None
    g = math.gcd(width, height)
    return f"{width // g}:{height // g}"


def _format_note_from_info(info: dict) -> str:
    """Build a human-readable format description from yt-dlp info_dict.
    Includes original WxH so the caller can log actual source dimensions.
    """
    width    = info.get("width")
    height   = info.get("height")
    vcodec   = info.get("vcodec", "")
    acodec   = info.get("acodec", "")
    fmt_id   = info.get("format_id", "")
    ext      = info.get("ext", "")
    parts    = []
    if width and height:
        parts.append(f"{width}x{height}")
    elif height:
        parts.append(f"{height}p")
    if ext:
        parts.append(ext.upper())
    if vcodec and vcodec != "none":
        parts.append(vcodec.split(".")[0])
    if acodec and acodec != "none":
        parts.append(acodec.split(".")[0])
    return " / ".join(parts) if parts else (fmt_id or "unknown")


# ─── yt-dlp provider — single attempt ────────────────────────────────────────

class _ProviderError(Exception):
    def __init__(self, error_type: str, raw: str):
        self.error_type = error_type
        self.raw        = raw
        super().__init__(raw)


def _ydlp_provider(
    url: str,
    out_base: str,
    fmt: str,
    vip_mode: bool = False,
) -> tuple[str, str, float, str, int | None, int | None]:
    """Single yt-dlp download attempt.

    FREE mode — uses _YDL_FORMAT_FREE, no FFmpeg required.
                Original dimensions are preserved — height<=720 selector removed.
    VIP  mode — uses _YDL_FORMAT_VIP, FFmpeg merges streams (remux, no re-encode).
                Highest available resolution at original aspect ratio.

    NO scale, crop, pad, or resize filters are ever applied.
    The returned width/height are the SOURCE values from yt-dlp info_dict.

    Returns (filepath, title, size_mb, format_note, width, height).
    Raises  _ProviderError on any failure.
    """
    ydl_opts: dict = {
        "format":      fmt,
        "outtmpl":     out_base + ".%(ext)s",
        "noplaylist":  True,
        "quiet":       True,
        "no_warnings": True,
        "socket_timeout": 30,
        "http_headers": _YDL_HEADERS,
    }

    if vip_mode and _FFMPEG:
        # Merge bestvideo+bestaudio into MP4 container without re-encoding
        ydl_opts["ffmpeg_location"]     = _FFMPEG
        ydl_opts["merge_output_format"] = "mp4"
        log.info(f"[FB] VIP mode — FFmpeg merge enabled ({_FFMPEG})")
    elif vip_mode and not _FFMPEG:
        # FFmpeg unavailable — fall back to free format silently
        log.warning("[FB] VIP mode requested but FFmpeg not found — using free format")
        ydl_opts["format"] = _YDL_FORMAT_FREE

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except _ydl_utils.DownloadError as exc:
        raw = str(exc)
        log.info(f"[FB] yt-dlp raw error: {raw[:200]}")
        raise _ProviderError(_classify(raw), raw)
    except _ydl_utils.ExtractorError as exc:
        raw = str(exc)
        log.info(f"[FB] yt-dlp extractor error: {raw[:200]}")
        raise _ProviderError("unsupported", raw)
    except Exception as exc:
        raw = str(exc)
        log.info(f"[FB] yt-dlp unexpected error: {raw[:200]}")
        raise _ProviderError("download_failed", raw)

    candidates = _glob.glob(out_base + ".*")
    if not candidates:
        raise _ProviderError("download_failed", "Output file not found after download")

    filepath    = candidates[0]
    _info       = info or {}
    title       = _info.get("title") or "Facebook Video"
    size_mb     = os.path.getsize(filepath) / (1024 * 1024)
    width       = _info.get("width")   or None
    height      = _info.get("height")  or None
    ratio       = _aspect_ratio_str(width, height)
    format_note = _format_note_from_info(_info)

    log.info(
        f"[FB] provider success — "
        f"dimensions={width}x{height} ratio={ratio} "
        f"format={format_note} size={size_mb:.2f}MB vip={vip_mode}"
    )
    return filepath, title, size_mb, format_note, width, height


# ─── Full provider pipeline ───────────────────────────────────────────────────

def _run_provider_pipeline(
    url: str,
    out_base: str,
    vip_mode: bool = False,
) -> DownloadResult:
    """Three-step pipeline.  Returns a DownloadResult — never raises."""

    fmt = _YDL_FORMAT_VIP if vip_mode else _YDL_FORMAT_FREE

    # ── Step 1: yt-dlp on original URL ───────────────────────────────────────
    log.info(f"[FB] pipeline step=1 vip={vip_mode} url={url}")
    try:
        fp, title, size_mb, fmt_note, w, h = _ydlp_provider(url, out_base, fmt, vip_mode)
        return DownloadResult(
            ok=True, filepath=fp, title=title, size_mb=size_mb,
            format_note=fmt_note, width=w, height=h,
            aspect_ratio=_aspect_ratio_str(w, h), vip_mode=vip_mode,
        )
    except _ProviderError as e1:
        log.info(f"[FB] pipeline step=1 FAIL type={e1.error_type}")
        last_err = e1

    # ── Step 2: login-redirect fallback ──────────────────────────────────────
    login_match = _LOGIN_URL_RE.search(last_err.raw)
    if login_match:
        story_url = _story_url_from_login(login_match.group(0))
        if story_url:
            log.info(f"[FB] pipeline step=2 story_url={story_url}")
            try:
                fp, title, size_mb, fmt_note, w, h = _ydlp_provider(
                    story_url, out_base, fmt, vip_mode
                )
                return DownloadResult(
                    ok=True, filepath=fp, title=title, size_mb=size_mb,
                    format_note=fmt_note, width=w, height=h,
                    aspect_ratio=_aspect_ratio_str(w, h), vip_mode=vip_mode,
                )
            except _ProviderError as e2:
                log.info(f"[FB] pipeline step=2 FAIL type={e2.error_type}")
                last_err = e2

    # ── Step 3: urllib Location-header fallback (share links only) ────────────
    if _SHARE_PATH_RE.search(url):
        log.info("[FB] pipeline step=3 urllib resolve")
        loc = _urllib_first_redirect(url)
        if loc and loc != url and "login" not in loc:
            log.info(f"[FB] pipeline step=3 resolved={loc}")
            try:
                fp, title, size_mb, fmt_note, w, h = _ydlp_provider(
                    loc, out_base, fmt, vip_mode
                )
                return DownloadResult(
                    ok=True, filepath=fp, title=title, size_mb=size_mb,
                    format_note=fmt_note, width=w, height=h,
                    aspect_ratio=_aspect_ratio_str(w, h), vip_mode=vip_mode,
                )
            except _ProviderError as e3:
                log.info(f"[FB] pipeline step=3 FAIL type={e3.error_type}")
                last_err = e3
        else:
            log.info("[FB] pipeline step=3 no usable Location header, skipping")

    return _fail(last_err.error_type, last_err.raw)


# ─── Public async entry point ─────────────────────────────────────────────────

async def download_facebook_video(
    url: str,
    uid: int,
    msg_id: int,
    vip_mode: bool = False,
) -> DownloadResult:
    """Async entry point for the Facebook provider.

    vip_mode=True  — highest available format, FFmpeg merge, send_document.
    vip_mode=False — best pre-merged MP4 (≤720p), send_video.

    Always returns a DownloadResult — never raises to the caller.
    Blocking I/O runs in a thread executor so the event loop is free.
    """
    mode_label = "VIP" if vip_mode else "FREE"
    log.info(f"[FB] download started — user={uid} mode={mode_label} url={url}")
    out_base = os.path.join(TEMP_DIR, f"fb_{uid}_{msg_id}")

    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(
            None, _run_provider_pipeline, url, out_base, vip_mode
        )
    except Exception as exc:
        log.error(f"[FB] executor error — user={uid}: {exc}")
        result = _fail("download_failed", str(exc))

    if result.ok:
        send_mode = "send_document" if vip_mode else "send_video"
        log.info(
            f"[FB] download complete — user={uid} mode={mode_label} "
            f"dimensions={result.width}x{result.height} "
            f"ratio={result.aspect_ratio} "
            f"format={result.format_note} size={result.size_mb:.2f}MB "
            f"send_mode={send_mode}"
        )
    else:
        log.warning(
            f"[FB] download failed — user={uid} mode={mode_label} "
            f"type={result.error_type}"
        )
    return result
