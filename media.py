"""Download creative media URLs and produce local image paths for grouping."""

import asyncio
import hashlib
import re
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
VIDEO_EXTENSIONS = {".mp4", ".webm", ".mov", ".m4v"}

ADVERTISER_COLUMN_CANDIDATES = (
    "advertiser_name",
    "advertise_name",
    "advertiserName",
    "advertiseName",
    "Advertiser_Name",
    "Advertise_Name",
    "AdvertiserName",
    "advertiser",
    "Advertiser",
    "brand_name",
    "brandName",
)

URL_COLUMN_CANDIDATES = (
    "url",
    "URL",
    "link",
    "Link",
    "media_url",
    "mediaUrl",
    "MediaURL",
    "creative_url",
    "creative_url_supplier",
    "creativeUrl",
    "image_url",
    "imageUrl",
    "video_url",
    "videoUrl",
    "ad_url",
    "adUrl",
)


def detect_advertiser_column(columns: list[str]) -> str | None:
    lower = {c.lower(): c for c in columns}
    for cand in ADVERTISER_COLUMN_CANDIDATES:
        if cand.lower() in lower:
            return lower[cand.lower()]
    for col in columns:
        cl = col.lower()
        if "advertis" in cl and cl != "brand":
            return col
    return None


def browser_headers_for_url(url: str) -> dict[str, str]:
    """Headers that improve CDN hotlink fetches (e.g. Unilever, Adclarity)."""
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()
    origin = f"{parsed.scheme or 'https'}://{parsed.netloc or host}"
    referer = f"{origin}/"
    if "adclarity" in host:
        referer = "https://ads.adclarity.com/"
    elif "tiktok" in host:
        referer = "https://www.tiktok.com/"
    accept = "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"
    if any(x in url.lower() for x in (".mp4", ".webm", ".mov", "video")):
        accept = "*/*"
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": accept,
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": referer,
    }


def is_adclarity_url(url: str) -> bool:
    return "adclarity.com" in url.lower()


def is_video_url(url: str) -> bool:
    lower = url.lower()
    path = urlparse(url).path.lower()
    return any(path.endswith(ext) for ext in VIDEO_EXTENSIONS) or "_video.mp4" in lower or ".mp4" in lower


def detect_url_column(columns: list[str]) -> str | None:
    lower = {c.lower(): c for c in columns}
    for cand in URL_COLUMN_CANDIDATES:
        if cand.lower() in lower:
            return lower[cand.lower()]
    for col in columns:
        if "url" in col.lower() or "link" in col.lower():
            return col
    return None


def _cache_key(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:16]


def youtube_video_id(url: str) -> str | None:
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower().replace("www.", "")
    if host in ("youtu.be",):
        vid = parsed.path.strip("/").split("/")[0]
        return vid or None
    if host in ("youtube.com", "m.youtube.com", "music.youtube.com"):
        if parsed.path == "/watch":
            return parse_qs(parsed.query).get("v", [None])[0]
        m = re.match(r"^/(embed|shorts|v)/([^/?]+)", parsed.path)
        if m:
            return m.group(2)
    return None


def tiktok_video_id(url: str) -> str | None:
    m = re.search(r"/video/(\d+)", url)
    return m.group(1) if m else None


def youtube_thumbnail_url(url: str) -> str | None:
    vid = youtube_video_id(url)
    if not vid:
        return None
    return f"https://img.youtube.com/vi/{vid}/hqdefault.jpg"


async def fetch_bytes(client: httpx.AsyncClient, url: str, timeout: float = 30.0) -> bytes | None:
    try:
        resp = await client.get(
            url,
            follow_redirects=True,
            timeout=timeout,
            headers=browser_headers_for_url(url),
        )
        resp.raise_for_status()
        return resp.content
    except Exception as e:
        print(f"Fetch failed {url}: {e}")
        return None


def save_bytes(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _ffmpeg_header_arg(url: str) -> list[str]:
    headers = browser_headers_for_url(url)
    header_str = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
    return ["-headers", header_str] if header_str else []


def extract_video_frame_from_url(url: str, out_path: Path) -> bool:
    """Grab one frame directly from a remote video URL (skips full MP4 download)."""
    try:
        cmd = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            *_ffmpeg_header_arg(url),
            "-ss",
            "0.5",
            "-i",
            url,
            "-vframes",
            "1",
            "-q:v",
            "2",
            str(out_path),
        ]
        subprocess.run(cmd, capture_output=True, check=True, timeout=60)
        return out_path.exists() and out_path.stat().st_size > 0
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        print(f"ffmpeg url frame failed {url[:80]}: {e}")
        return False


def extract_video_frame(video_path: Path, out_path: Path) -> bool:
    """Extract a poster frame via ffmpeg (fast seek for large Adclarity MP4s)."""
    try:
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                "0.5",
                "-i",
                str(video_path),
                "-vframes",
                "1",
                "-q:v",
                "2",
                str(out_path),
            ],
            capture_output=True,
            check=True,
            timeout=60,
        )
        return out_path.exists() and out_path.stat().st_size > 0
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        print(f"ffmpeg frame extract failed {video_path}: {e}")
        return False


def _fetch_bytes_sync(client: httpx.Client, url: str, timeout: float = 30.0) -> bytes | None:
    try:
        resp = client.get(
            url,
            follow_redirects=True,
            timeout=timeout,
            headers=browser_headers_for_url(url),
        )
        resp.raise_for_status()
        return resp.content
    except Exception as e:
        print(f"Fetch failed {url}: {e}")
        return None


def resolve_thumbnail_blocking(url: str, cache_dir: Path) -> Path | None:
    """Sync thumbnail resolver for use in asyncio.to_thread (won't block event loop)."""
    if not url:
        return None

    cache_dir = Path(cache_dir)
    key = _cache_key(url)
    cache_dir.mkdir(parents=True, exist_ok=True)

    thumb = youtube_thumbnail_url(url)
    if thumb:
        out = cache_dir / f"{key}_yt.jpg"
        if out.exists():
            return out
        with httpx.Client(follow_redirects=True, timeout=30.0) as client:
            data = _fetch_bytes_sync(client, thumb)
        if data:
            return save_bytes(out, data)

    if "tiktok.com" in url.lower() and "adclarity" not in url.lower():
        from urllib.parse import quote

        oembed = f"https://www.tiktok.com/oembed?url={quote(url, safe='')}"
        try:
            with httpx.Client(follow_redirects=True, timeout=25.0) as client:
                resp = client.get(oembed, headers=browser_headers_for_url("https://www.tiktok.com/"))
            if resp.status_code == 200:
                thumb_url = resp.json().get("thumbnail_url")
                if thumb_url:
                    out = cache_dir / f"{key}_tt.jpg"
                    if out.exists():
                        return out
                    with httpx.Client(follow_redirects=True, timeout=30.0) as client:
                        data = _fetch_bytes_sync(client, thumb_url)
                    if data:
                        return save_bytes(out, data)
        except Exception as e:
            print(f"TikTok oEmbed failed {url}: {e}")

    ext = extension_from_url(url)
    out = cache_dir / f"{key}{ext}"
    thumb_jpg = cache_dir / f"{key}_thumb.jpg"
    video_timeout = 120.0 if is_adclarity_url(url) else 90.0

    if ext in IMAGE_EXTENSIONS and not is_video_url(url):
        if out.exists():
            return out
        with httpx.Client(follow_redirects=True, timeout=video_timeout) as client:
            data = _fetch_bytes_sync(client, url, timeout=video_timeout)
        if data:
            return save_bytes(out, data)
        return None

    if is_video_url(url) or ext in VIDEO_EXTENSIONS:
        if thumb_jpg.exists():
            return thumb_jpg
        if extract_video_frame_from_url(url, thumb_jpg):
            return thumb_jpg
        if not out.exists():
            with httpx.Client(follow_redirects=True, timeout=video_timeout) as client:
                data = _fetch_bytes_sync(client, url, timeout=video_timeout)
            if not data:
                return None
            save_bytes(out, data)
        if extract_video_frame(out, thumb_jpg):
            return thumb_jpg
        return None

    if thumb_jpg.exists():
        return thumb_jpg
    with httpx.Client(follow_redirects=True, timeout=video_timeout) as client:
        data = _fetch_bytes_sync(client, url, timeout=video_timeout)
    if not data:
        return None
    guessed = extension_from_url(url)
    if guessed in VIDEO_EXTENSIONS:
        vid_path = cache_dir / f"{key}{guessed}"
        save_bytes(vid_path, data)
        if extract_video_frame(vid_path, thumb_jpg):
            return thumb_jpg
        return None
    img_path = cache_dir / f"{key}{guessed}"
    return save_bytes(img_path, data)


def extension_from_url(url: str, content_type: str | None = None) -> str:
    path = urlparse(url).path.lower()
    for ext in IMAGE_EXTENSIONS | VIDEO_EXTENSIONS:
        if path.endswith(ext):
            return ext
    if content_type:
        if "jpeg" in content_type or "jpg" in content_type:
            return ".jpg"
        if "png" in content_type:
            return ".png"
        if "gif" in content_type:
            return ".gif"
        if "webp" in content_type:
            return ".webp"
        if "mp4" in content_type:
            return ".mp4"
    return ".jpg"


async def resolve_thumbnail(
    client: httpx.AsyncClient,
    url: str,
    cache_dir: Path,
) -> Path | None:
    """Download or derive a local image file for grouping/display."""
    key = _cache_key(url)
    cache_dir.mkdir(parents=True, exist_ok=True)

    # YouTube page / watch URL
    thumb = youtube_thumbnail_url(url)
    if thumb:
        out = cache_dir / f"{key}_yt.jpg"
        if out.exists():
            return out
        data = await fetch_bytes(client, thumb)
        if data:
            return save_bytes(out, data)

    # TikTok page URL (not Adclarity-hosted)
    if "tiktok.com" in url.lower() and "adclarity" not in url.lower():
        from urllib.parse import quote

        oembed = f"https://www.tiktok.com/oembed?url={quote(url, safe='')}"
        try:
            resp = await client.get(
                oembed,
                timeout=25,
                headers=browser_headers_for_url("https://www.tiktok.com/"),
            )
            if resp.status_code == 200:
                thumb_url = resp.json().get("thumbnail_url")
                if thumb_url:
                    out = cache_dir / f"{key}_tt.jpg"
                    if out.exists():
                        return out
                    data = await fetch_bytes(client, thumb_url)
                    if data:
                        return save_bytes(out, data)
        except Exception as e:
            print(f"TikTok oEmbed failed {url}: {e}")

    # Direct fetch
    ext = extension_from_url(url)
    out = cache_dir / f"{key}{ext}"
    thumb_jpg = cache_dir / f"{key}_thumb.jpg"
    video_timeout = 180.0 if is_adclarity_url(url) else 120.0

    if ext in IMAGE_EXTENSIONS and not is_video_url(url):
        if out.exists():
            return out
        data = await fetch_bytes(client, url)
        if data:
            return save_bytes(out, data)
        return None

    if is_video_url(url) or ext in VIDEO_EXTENSIONS:
        if thumb_jpg.exists():
            return thumb_jpg
        if await asyncio.to_thread(extract_video_frame_from_url, url, thumb_jpg):
            return thumb_jpg
        if not out.exists():
            data = await fetch_bytes(client, url, timeout=video_timeout)
            if not data:
                return None
            save_bytes(out, data)
        if await asyncio.to_thread(extract_video_frame, out, thumb_jpg):
            return thumb_jpg
        return None

    # Unknown: try as image
    if thumb_jpg.exists():
        return thumb_jpg
    data = await fetch_bytes(client, url)
    if not data:
        return None
    ct = None
    guessed = extension_from_url(url, ct)
    if guessed in VIDEO_EXTENSIONS:
        vid_path = cache_dir / f"{key}{guessed}"
        save_bytes(vid_path, data)
        if extract_video_frame(vid_path, thumb_jpg):
            return thumb_jpg
        return None
    img_path = cache_dir / f"{key}{guessed}"
    return save_bytes(img_path, data)
