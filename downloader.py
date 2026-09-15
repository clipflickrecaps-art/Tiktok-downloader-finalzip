import re
import os
import asyncio
import time
import aiohttp
from logger import log

TEMP_DIR = "temp_media"
os.makedirs(TEMP_DIR, exist_ok=True)
MAX_DOWNLOAD_BYTES = int(os.environ.get("MAX_DOWNLOAD_MB", "100")) * 1024 * 1024

TIKTOK_PATTERN = re.compile(
    r'https?://(?:www\.|vm\.|vt\.|m\.)?tiktok\.com/\S+'
)

API_URL = "https://www.tikwm.com/api/"

USER_MESSAGES = {
    "invalid_url":        "❌ TikTok link မမှန်ကန်ပါ။ ဗီဒီယို link ကို ကူးယူပြီး ထပ်ပေးပါ။",
    "timeout":            "⏱ Server ချိတ်ဆက်မရပါ။ နည်းနည်း စောင့်ပြီး ထပ်ကြိုးစားပါ။",
    "api_error":          "⚠️ Download service အမှားဖြစ်သွားသည်။ နောက်မှ ထပ်ကြိုးစားပါ။",
    "private_video":      "🔒 ဤဗီဒီယိုသည် Private ဖြစ်နေသဖြင့် ဒေါင်းမရပါ။",
    "unavailable":        "❌ ဤဗီဒီယိုကို ရရှိမရပါ (ဖျက်သိမ်းပြီး သို့မဟုတ် မတည်ရှိတော့ပါ)။",
    "missing_url":        "⚠️ ဗီဒီယို URL ရှာမတွေ့ပါ။ နောက်မှ ထပ်ကြိုးစားပါ။",
    "malformed_response": "⚠️ Server မှ မမှန်ကန်သော တုံ့ပြန်မှုရရှိသည်။ နောက်မှ ထပ်ကြိုးစားပါ။",
    "download_failed":    "⚠️ ဗီဒီယိုဒေါင်းမရပါ။ နောက်မှ ထပ်ကြိုးစားပါ။",
    "file_too_large":     "⚠️ ဖိုင်အရွယ်အစားက သတ်မှတ်ထားသော limit ထက်ကြီးနေပါသည်။",
    "image_failed":       "❌ Image ဒေါင်းမရပါ။ နောက်မှ ထပ်ကြိုးစားပါ။",
    "live_photo_failed":  "❌ Live Photo ဒေါင်းမရပါ။ နောက်မှ ထပ်ကြိုးစားပါ။",
}


class DownloadError(Exception):
    def __init__(self, error_type: str, detail: str = ""):
        self.error_type = error_type
        self.detail = detail
        super().__init__(f"{error_type}: {detail}")

    def user_message(self) -> str:
        return USER_MESSAGES.get(self.error_type, USER_MESSAGES["api_error"])


def extract_url(text: str):
    match = TIKTOK_PATTERN.search(text)
    return match.group(0) if match else None


def is_valid_tiktok_url(url: str) -> bool:
    return bool(TIKTOK_PATTERN.fullmatch(url.strip()))


def get_original_video_url(data: dict) -> str | None:
    """Return the provider's highest-quality no-watermark URL, if exposed."""
    for field in ("hdplay", "hd_play", "original", "download", "no_watermark"):
        value = data.get(field)
        if isinstance(value, str) and re.match(r"^https?://", value):
            return value
    return None


def get_best_video_url(data: dict) -> str | None:
    """Return the best available URL, falling back to the normal play URL."""
    return get_original_video_url(data) or (
        data.get("play") if isinstance(data.get("play"), str) else None
    )


def get_live_photo_video_urls(data: dict) -> list:
    """Extract live photo video (MP4) URLs from API response.

    TikTok Live Photos are slideshow posts where each image has an associated
    short MP4 clip.  The tikwm API may expose these through several fields —
    we check all known locations in priority order.

    Returns a list of MP4 URL strings (may be empty if not a live photo post).
    """
    # Priority 1: top-level list field (most common)
    for field in ("live_photo_image_list", "image_videos", "live_photo_list"):
        urls = data.get(field)
        if urls and isinstance(urls, list) and len(urls) > 0:
            result = []
            for item in urls:
                if isinstance(item, str) and item:
                    result.append(item)
                elif isinstance(item, dict):
                    url = (item.get("url") or item.get("playUrl")
                           or item.get("play_url") or item.get("download_url"))
                    if url:
                        result.append(url)
            if result:
                log.info(f"Live photo URLs found in field '{field}': {len(result)} clip(s)")
                return result

    # Priority 2: image_post_info array — each item may have a 'video' sub-dict
    image_post_info = data.get("image_post_info")
    if image_post_info and isinstance(image_post_info, list):
        result = []
        for item in image_post_info:
            if not isinstance(item, dict):
                continue
            video = item.get("video") or {}
            url = (video.get("playUrl") or video.get("play_url")
                   or video.get("url") or video.get("download_url"))
            if url:
                result.append(url)
        if result:
            log.info(f"Live photo URLs found in 'image_post_info': {len(result)} clip(s)")
            return result

    return []


def get_content_type(data: dict) -> str:
    """Return content type string for the API response data.

    'live_photo' — slideshow post where each image has an associated MP4 clip
    'image'      — regular photo carousel / slideshow (no live video)
    'video'      — standard video post
    """
    images = data.get("images")
    if images and isinstance(images, list) and len(images) > 0:
        if get_live_photo_video_urls(data):
            return "live_photo"
        return "image"
    return "video"


def check_image_access(user_id: int) -> bool:
    """Gate for image/photo download access.

    CURRENT BEHAVIOUR: always True — image download is free for all users.

    FUTURE (after 1000 users): check premium status or ad completion before
    returning True.  Activate by replacing the body of this function; do NOT
    change call sites.
    """
    return True


async def fetch_tiktok_data(url: str, hd: bool = False) -> dict:
    log.info(f"API request started for: {url}")
    try:
        params = {"url": url}
        if hd:
            params["hd"] = "1"
        async with aiohttp.ClientSession() as session:
            async with session.get(
                API_URL,
                params=params,
                timeout=aiohttp.ClientTimeout(total=15)
            ) as response:
                if response.status == 429:
                    log.warning(f"TikTok API rate limited for: {url}")
                    raise DownloadError("rate_limited", "Provider rate limit")
                if response.status != 200:
                    log.error(f"API HTTP {response.status} for: {url}")
                    raise DownloadError("api_error", f"HTTP {response.status}")

                try:
                    res = await response.json(content_type=None)
                except Exception as parse_err:
                    log.error(f"JSON parse error for {url}: {parse_err}")
                    raise DownloadError("malformed_response", str(parse_err))

    except asyncio.TimeoutError:
        log.error(f"API request timed out for: {url}")
        raise DownloadError("timeout", "Request timed out")
    except DownloadError:
        raise
    except Exception as e:
        log.error(f"Unexpected network error for {url}: {e}")
        raise DownloadError("api_error", str(e))

    if not isinstance(res, dict):
        log.error(f"TikTok API returned non-object response for: {url}")
        raise DownloadError("malformed_response", "Invalid API response")
    code = res.get("code")
    msg = str(res.get("msg", "")).lower()
    data = res.get("data")
    if data is not None and not isinstance(data, dict):
        log.error(f"TikTok API returned invalid data type for: {url}")
        raise DownloadError("malformed_response", "Invalid API data")

    if not data or code == -1:
        log.warning(f"API no-data response for {url} — code={code} msg={msg}")
        if "private" in msg:
            raise DownloadError("private_video", msg)
        if "not found" in msg or "deleted" in msg or "unavailable" in msg:
            raise DownloadError("unavailable", msg)
        raise DownloadError("api_error", msg)

    content_type = get_content_type(data)
    log.info(f"API response parsed — video id: {data.get('id')}, "
             f"type: {content_type}, "
             f"size: {round((data.get('size') or 0) / (1024*1024), 2)} MB")

    if content_type == "video" and not data.get("play"):
        log.warning(f"Missing play URL in video response for: {url}")
        raise DownloadError("missing_url", "No play URL")
    if content_type == "video" and not re.match(r"^https?://", str(data.get("play", ""))):
        raise DownloadError("missing_url", "Invalid play URL")

    return data


async def download_to_file(url: str, filepath: str) -> int:
    """Stream media from url into filepath. Returns file size in bytes."""
    log.info(f"Temp download started → {filepath}")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=60)
            ) as resp:
                if resp.status != 200:
                    log.error(f"Media download HTTP {resp.status} for: {url}")
                    raise DownloadError("download_failed", f"HTTP {resp.status}")
                content_length = resp.headers.get("Content-Length")
                if content_length and int(content_length) > MAX_DOWNLOAD_BYTES:
                    raise DownloadError("file_too_large", "Download exceeds configured size limit")
                total = 0
                with open(filepath, "wb") as f:
                    async for chunk in resp.content.iter_chunked(1024 * 64):
                        total += len(chunk)
                        if total > MAX_DOWNLOAD_BYTES:
                            raise DownloadError("file_too_large", "Download exceeds configured size limit")
                        f.write(chunk)
    except asyncio.TimeoutError:
        log.error(f"Media download timed out: {url}")
        raise DownloadError("timeout", "Download timed out")
    except DownloadError:
        cleanup_file(filepath)
        raise
    except Exception as e:
        cleanup_file(filepath)
        log.error(f"Unexpected error downloading media: {e}")
        raise DownloadError("download_failed", str(e))

    size = os.path.getsize(filepath)
    log.info(f"Temp download complete — {filepath} ({round(size / (1024*1024), 2)} MB)")
    return size


def cleanup_file(filepath: str):
    try:
        if os.path.exists(filepath):
            os.remove(filepath)
            log.info(f"Temp file deleted: {filepath}")
    except Exception as e:
        log.warning(f"Failed to delete temp file {filepath}: {e}")


def cleanup_files(paths) -> None:
    """Delete a list of temp file paths.  Skips None entries silently."""
    for p in paths:
        if p:
            cleanup_file(p)


def cleanup_stale_files(max_age_seconds: int = 3600) -> int:
    """Delete abandoned files left in temp_media after a crash or restart."""
    now = time.time()
    removed = 0
    for name in os.listdir(TEMP_DIR):
        path = os.path.join(TEMP_DIR, name)
        try:
            if os.path.isfile(path) and now - os.path.getmtime(path) > max_age_seconds:
                os.remove(path)
                removed += 1
        except OSError:
            continue
    if removed:
        log.info("Removed %d stale temporary media file(s)", removed)
    return removed


_IMAGE_HEADERS = {
    "Referer":    "https://www.tiktok.com/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}


async def download_live_photos_to_files(video_urls: list, uid: int) -> list:
    """Download live photo MP4 clips concurrently to temp files.

    Returns a list parallel to video_urls.  Each element is either a file
    path (str) on success or None on failure.
    """
    async def _fetch_one(session: aiohttp.ClientSession, url: str, idx: int):
        filepath = os.path.join(TEMP_DIR, f"lp_{uid}_{idx}.mp4")
        try:
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=60),
                headers=_IMAGE_HEADERS,
            ) as resp:
                if resp.status != 200:
                    log.error(
                        f"Live photo {idx + 1} HTTP {resp.status} "
                        f"for user {uid}: {url[:80]}"
                    )
                    return None
                with open(filepath, "wb") as f:
                    async for chunk in resp.content.iter_chunked(65536):
                        f.write(chunk)
            log.info(
                f"Live photo {idx + 1}/{len(video_urls)} downloaded "
                f"for user {uid}: {filepath}"
            )
            return filepath
        except Exception as e:
            log.error(f"Live photo {idx + 1} download error for user {uid}: {e}")
            return None

    async with aiohttp.ClientSession() as session:
        results = await asyncio.gather(
            *[_fetch_one(session, url, i) for i, url in enumerate(video_urls)]
        )
    return list(results)


async def download_images_to_files(image_urls: list, uid: int) -> list:
    """Download every image URL concurrently to a temp file.

    Returns a list parallel to image_urls.  Each element is either a file
    path (str) on success or None on failure.  This uses the same pattern as
    download_to_file so Telegram receives local files rather than CDN URLs,
    avoiding CDN auth / Referer restrictions.
    """
    async def _fetch_one(session: aiohttp.ClientSession, url: str, idx: int):
        filepath = os.path.join(TEMP_DIR, f"img_{uid}_{idx}.jpg")
        try:
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=30),
                headers=_IMAGE_HEADERS,
            ) as resp:
                if resp.status != 200:
                    log.error(
                        f"Image {idx + 1} HTTP {resp.status} "
                        f"for user {uid}: {url[:80]}"
                    )
                    return None
                with open(filepath, "wb") as f:
                    async for chunk in resp.content.iter_chunked(65536):
                        f.write(chunk)
            log.info(
                f"Image {idx + 1}/{len(image_urls)} downloaded "
                f"for user {uid}: {filepath}"
            )
            return filepath
        except Exception as e:
            log.error(f"Image {idx + 1} download error for user {uid}: {e}")
            return None

    async with aiohttp.ClientSession() as session:
        results = await asyncio.gather(
            *[_fetch_one(session, url, i) for i, url in enumerate(image_urls)]
        )
    return list(results)
