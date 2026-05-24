"""Download creative media URLs and produce local image paths for grouping."""

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
        if "advertis" in cl or cl == "brand" or "brand_name" in cl:
            return col
    return None


def browser_headers_for_url(url: str) -> dict[str, str]:
    """Headers that improve CDN hotlink fetches (e.g. Unilever assets)."""
    parsed = urlparse(url)
    host = parsed.netloc or ""
    origin = f"{parsed.scheme or 'https'}://{host}"
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": f"{origin}/",
    }


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


def extract_video_frame(video_path: Path, out_path: Path) -> bool:
    """Extract first frame via ffmpeg if available."""
    try:
        subprocess.run(
            [
                "ffmpeg",
                "-y",
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
        return out_path.exists()
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False


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

    # TikTok oEmbed thumbnail
    if "tiktok.com" in url.lower():
        oembed = f"https://www.tiktok.com/oembed?url={httpx.URL(url)}"
        try:
            resp = await client.get(oembed, timeout=20)
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

    if ext in IMAGE_EXTENSIONS:
        if out.exists():
            return out
        data = await fetch_bytes(client, url)
        if data:
            return save_bytes(out, data)
        return None

    if ext in VIDEO_EXTENSIONS or ".mp4" in url.lower():
        if thumb_jpg.exists():
            return thumb_jpg
        if not out.exists():
            data = await fetch_bytes(client, url, timeout=120)
            if not data:
                return None
            save_bytes(out, data)
        if extract_video_frame(out, thumb_jpg):
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
