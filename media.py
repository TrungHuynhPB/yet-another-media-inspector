"""Download creative media URLs and produce local image paths for grouping."""

import asyncio
import hashlib
import os
import re
import sys
import threading
import time
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

DEFAULT_THUMBNAIL_WORKERS = 24
PARTIAL_VIDEO_BYTES = 2 * 1024 * 1024
CHROME_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
ADCLARITY_ORIGIN = "https://ads.adclarity.com"

_thread_local = threading.local()
_host_semaphores: dict[str, threading.Semaphore] = {}
_host_sem_lock = threading.Lock()

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


_CV2 = None
_CV2_LOAD_ERROR: str | None = None


def _ensure_cv2():
    """Load OpenCV once; cache success or the real load error (not only ImportError)."""
    global _CV2, _CV2_LOAD_ERROR
    if _CV2 is not None:
        return _CV2
    if _CV2_LOAD_ERROR is not None:
        return None
    try:
        import cv2

        _CV2 = cv2
        return _CV2
    except Exception as exc:
        _CV2_LOAD_ERROR = f"{type(exc).__name__}: {exc}"
        return None


def opencv_available() -> bool:
    return _ensure_cv2() is not None


def opencv_unavailable_reason() -> str:
    _ensure_cv2()
    if _CV2 is not None:
        return ""
    err = _CV2_LOAD_ERROR or "unknown import error"
    return (
        f"opencv-python not available in this Python ({sys.executable}): {err}. "
        f"Install with: {sys.executable} -m pip install opencv-python"
    )


def opencv_diagnostics() -> dict:
    _ensure_cv2()
    cv2 = _CV2
    info: dict = {
        "pythonExecutable": sys.executable,
        "opencvAvailable": cv2 is not None,
    }
    if cv2 is not None:
        info["opencvVersion"] = getattr(cv2, "__version__", "")
    else:
        info["error"] = _CV2_LOAD_ERROR or "cv2 import failed"
    return info


def adclarity_video_download_timeout() -> float:
    for key in ("YAMI_ADCLARITY_VIDEO_TIMEOUT", "YAMI_ADCLARITY_FFMPEG_TIMEOUT"):
        raw = os.environ.get(key)
        if raw:
            try:
                return float(raw)
            except ValueError:
                pass
    return 120.0


def adclarity_max_parallel() -> int:
    try:
        n = int(os.environ.get("YAMI_ADCLARITY_MAX_PARALLEL", "4"))
    except ValueError:
        n = 4
    return max(1, min(n, 16))


@contextmanager
def _host_request_slot(url: str):
    """Limit concurrent requests per AdClarity CDN host."""
    if not is_adclarity_url(url):
        yield
        return
    host = urlparse(url).netloc.lower()
    with _host_sem_lock:
        if host not in _host_semaphores:
            _host_semaphores[host] = threading.Semaphore(adclarity_max_parallel())
        sem = _host_semaphores[host]
    sem.acquire()
    try:
        yield
    finally:
        sem.release()


def browser_headers_for_url(url: str) -> dict[str, str]:
    """Headers that improve CDN hotlink fetches (e.g. Unilever, Adclarity)."""
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()
    origin = f"{parsed.scheme or 'https'}://{parsed.netloc or host}"
    referer = f"{origin}/"
    extra: dict[str, str] = {}
    if "adclarity" in host:
        referer = f"{ADCLARITY_ORIGIN}/"
        extra = {
            "Origin": ADCLARITY_ORIGIN,
            "Sec-Fetch-Dest": "video" if is_video_url(url) else "image",
            "Sec-Fetch-Mode": "no-cors",
            "Sec-Fetch-Site": "cross-site",
        }
    elif "tiktok" in host:
        referer = "https://www.tiktok.com/"
    accept = "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"
    if any(x in url.lower() for x in (".mp4", ".webm", ".mov", "video")):
        accept = "*/*"
    return {
        "User-Agent": CHROME_USER_AGENT,
        "Accept": accept,
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": referer,
        **extra,
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
    """Best-effort single YouTube poster URL (see youtube_thumbnail_urls for fallbacks)."""
    urls = youtube_thumbnail_urls(url)
    return urls[2] if len(urls) > 2 else (urls[0] if urls else None)


def is_youtube_url(url: str) -> bool:
    lower = url.lower()
    if "youtu.be" in lower or "youtube.com" in lower or "img.youtube.com" in lower:
        return True
    return youtube_video_id(url) is not None


def is_tiktok_page_url(url: str) -> bool:
    return "tiktok.com" in url.lower() and "adclarity" not in url.lower()


def youtube_thumbnail_urls(url: str) -> list[str]:
    vid = youtube_video_id(url)
    if not vid:
        m = re.search(r"img\.youtube\.com/vi/([^/]+)/", url, re.I)
        if m:
            vid = m.group(1)
    if not vid:
        return []
    return [
        f"https://img.youtube.com/vi/{vid}/maxresdefault.jpg",
        f"https://img.youtube.com/vi/{vid}/sddefault.jpg",
        f"https://img.youtube.com/vi/{vid}/hqdefault.jpg",
        f"https://img.youtube.com/vi/{vid}/mqdefault.jpg",
        f"https://img.youtube.com/vi/{vid}/default.jpg",
    ]


def tiktok_extract_info(url: str) -> tuple[str | None, str | None]:
    try:
        parts = urlparse(url).path.strip("/").split("/")
        if len(parts) < 2:
            return None, None
        channel = parts[0].lstrip("@")
        video_id = parts[-1]
        if not str(video_id).isdigit():
            video_id = tiktok_video_id(url) or video_id
        if not channel or not video_id:
            return None, None
        return channel, video_id
    except Exception:
        return None, None


def tiktok_oembed_url(url: str) -> str:
    channel, video_id = tiktok_extract_info(url)
    if channel and video_id:
        page = f"https://www.tiktok.com/@{channel}/video/{video_id}"
        return f"https://www.tiktok.com/oembed?url={page}"
    from urllib.parse import quote

    return f"https://www.tiktok.com/oembed?url={quote(url, safe='')}"


def adclarity_jpeg_from_mp4_url(url: str) -> str | None:
    """
    AdClarity MP4 → JPEG sibling URL: drop `_video` before `.mp4`, use `.jpeg`.
    e.g. …/abc_video.mp4 → …/abc.jpeg
    """
    if not is_adclarity_url(url) or ".mp4" not in url.lower():
        return None
    base, sep, query = url.partition("?")
    path = re.sub(r"_video(?=\.mp4)", "", base, flags=re.IGNORECASE)
    path = re.sub(r"\.mp4$", ".jpeg", path, flags=re.IGNORECASE)
    if not path.lower().endswith(".jpeg"):
        return None
    jpeg_url = path + (sep + query if sep else "")
    if jpeg_url == url or is_video_url(jpeg_url):
        return None
    return jpeg_url


def adclarity_thumbnail_candidates(url: str, max_candidates: int = 3) -> list[str]:
    """Static AdClarity poster URLs to try before downloading MP4 / OpenCV."""
    if not is_adclarity_url(url):
        return []
    candidates: list[str] = []
    seen: set[str] = set()

    def add(candidate: str) -> None:
        if candidate and candidate not in seen and not is_video_url(candidate):
            seen.add(candidate)
            candidates.append(candidate)

    add(adclarity_jpeg_from_mp4_url(url) or "")
    primary = candidates[0] if candidates else None
    if primary and primary.lower().endswith(".jpeg"):
        add(re.sub(r"\.jpeg(\?.*)?$", r".jpg\1", primary, flags=re.IGNORECASE))
    return candidates[:max_candidates]


def _probe_media_url(client: httpx.Client, url: str) -> tuple[bool, str]:
    """HEAD or small Range GET to confirm the video URL is reachable."""
    headers = browser_headers_for_url(url)
    try:
        resp = client.head(url, headers=headers, timeout=15.0, follow_redirects=True)
        if resp.status_code in (200, 206):
            return True, ""
        if resp.status_code == 403:
            return False, "HTTP 403 from CDN"
        if resp.status_code == 429:
            return False, "HTTP 429 rate limited"
        if resp.status_code == 404:
            return False, "HTTP 404 video not found"
    except httpx.TimeoutException:
        return False, "HTTP timeout probing video URL"
    except httpx.HTTPError as exc:
        return False, f"HTTP error probing video: {exc}"

    try:
        range_headers = {**headers, "Range": "bytes=0-65535"}
        resp = client.get(
            url, headers=range_headers, timeout=20.0, follow_redirects=True
        )
        if resp.status_code in (200, 206):
            return True, ""
        if resp.status_code == 403:
            return False, "HTTP 403 from CDN"
        if resp.status_code == 429:
            return False, "HTTP 429 rate limited"
        return False, f"HTTP {resp.status_code} probing video"
    except httpx.TimeoutException:
        return False, "HTTP timeout probing video URL"
    except httpx.HTTPError as exc:
        return False, f"HTTP error probing video: {exc}"


def _http_get_bytes(
    client: httpx.Client,
    url: str,
    *,
    timeout: float,
    range_header: str | None = None,
) -> tuple[bytes | None, str | None]:
    """GET with optional retry on 403/429 for AdClarity."""
    headers = dict(browser_headers_for_url(url))
    if range_header:
        headers["Range"] = range_header
    attempts = 2 if is_adclarity_url(url) else 1
    last_detail: str | None = None
    for attempt in range(attempts):
        try:
            resp = client.get(
                url, headers=headers, timeout=timeout, follow_redirects=True
            )
            if resp.status_code in (403, 429) and attempt < attempts - 1:
                time.sleep(1.5)
                last_detail = f"HTTP {resp.status_code} from CDN"
                continue
            resp.raise_for_status()
            return resp.content, None
        except httpx.TimeoutException:
            last_detail = "HTTP timeout downloading media"
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            last_detail = f"HTTP {code} from CDN"
            if code in (403, 429) and attempt < attempts - 1:
                time.sleep(1.5)
                continue
        except httpx.HTTPError as exc:
            last_detail = f"HTTP error: {exc}"
    return None, last_detail


def is_image_bytes(data: bytes) -> bool:
    if not data or len(data) < 200:
        return False
    head = data.lstrip()[:32].lower()
    if head.startswith((b"<!doctype", b"<html", b"<?xml")) or head.startswith(b"{"):
        return False
    if data[:3] == b"\xff\xd8\xff":
        return True
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return True
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return True
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return True
    return False


def _existing_cached_thumb(cache_dir: Path, key: str) -> Path | None:
    for name in (f"{key}_thumb.jpg", f"{key}_yt.jpg", f"{key}_tt.jpg"):
        path = cache_dir / name
        if path.is_file() and path.stat().st_size > 0:
            return path
    return None


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


def extract_video_frame(video_path: Path, out_path: Path) -> tuple[bool, str | None]:
    """Extract a poster frame from a local video file using OpenCV."""
    cv2 = _ensure_cv2()
    if cv2 is None:
        return False, opencv_unavailable_reason()
    try:
        cap = cv2.VideoCapture(str(video_path))
        if not cap.isOpened():
            cap.release()
            return False, "could not open video file"

        fps = cap.get(cv2.CAP_PROP_FPS)
        if fps and fps > 0:
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(fps * 0.5))

        success, frame = cap.read()
        if not success or frame is None:
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            success, frame = cap.read()
        cap.release()

        if not success or frame is None:
            return False, "could not read video frame"

        out_path.parent.mkdir(parents=True, exist_ok=True)
        if not cv2.imwrite(str(out_path), frame):
            return False, "could not write thumbnail image"
        if out_path.exists() and out_path.stat().st_size > 0:
            return True, None
        return False, "thumbnail file was not created"
    except Exception as exc:
        print(f"OpenCV frame extract failed {video_path}: {exc}")
        return False, f"frame extraction failed: {exc}"


def _fetch_bytes_sync(
    client: httpx.Client, url: str, timeout: float = 30.0
) -> tuple[bytes | None, str | None]:
    return _http_get_bytes(client, url, timeout=timeout)


def _try_download_image(
    client: httpx.Client, url: str, timeout: float = 30.0
) -> bytes | None:
    data, _detail = _fetch_bytes_sync(client, url, timeout=timeout)
    if data and is_image_bytes(data):
        return data
    return None


def _fetch_partial_video(
    client: httpx.Client,
    url: str,
    cache_dir: Path,
    key: str,
    ext: str,
) -> tuple[Path | None, str | None]:
    """Download first ~2MB via Range for local OpenCV frame extraction."""
    range_hdr = f"bytes=0-{PARTIAL_VIDEO_BYTES - 1}"
    data, detail = _http_get_bytes(
        client, url, timeout=60.0, range_header=range_hdr
    )
    if not data:
        return None, detail or "partial video download failed"
    vid_path = cache_dir / f"{key}_partial{ext}"
    save_bytes(vid_path, data)
    return vid_path, None


def _try_urls_as_image(
    client: httpx.Client,
    urls: list[str],
    cache_dir: Path,
    key: str,
    tag: str,
    timeout: float = 25.0,
) -> Path | None:
    out = cache_dir / f"{key}_{tag}.jpg"
    if out.is_file() and out.stat().st_size > 0:
        return out
    for candidate in urls:
        data = _try_download_image(client, candidate, timeout=timeout)
        if data:
            return save_bytes(out, data)
    return None


def _resolve_youtube_thumbnail(
    client: httpx.Client, url: str, cache_dir: Path, key: str
) -> Path | None:
    urls = youtube_thumbnail_urls(url)
    if not urls:
        return None
    return _try_urls_as_image(client, urls, cache_dir, key, "yt", timeout=20.0)


def _resolve_tiktok_thumbnail(
    client: httpx.Client, url: str, cache_dir: Path, key: str
) -> Path | None:
    out = cache_dir / f"{key}_tt.jpg"
    if out.is_file() and out.stat().st_size > 0:
        return out
    oembed = tiktok_oembed_url(url)
    try:
        resp = client.get(
            oembed,
            timeout=15.0,
            headers=browser_headers_for_url("https://www.tiktok.com/"),
        )
        if resp.status_code != 200:
            return None
        thumb_url = resp.json().get("thumbnail_url")
        if not thumb_url:
            return None
        data = _try_download_image(client, thumb_url, timeout=25.0)
        if data:
            return save_bytes(out, data)
    except Exception as e:
        print(f"TikTok oEmbed failed {url}: {e}")
    return None


def _resolve_video_poster(
    client: httpx.Client,
    url: str,
    cache_dir: Path,
    key: str,
    static_candidates: list[str] | None = None,
) -> tuple[Path | None, str | None]:
    """Prefer AdClarity .jpeg sibling URL, then other static posters, then OpenCV."""
    thumb_jpg = cache_dir / f"{key}_thumb.jpg"
    if thumb_jpg.is_file() and thumb_jpg.stat().st_size > 0:
        return thumb_jpg, None

    image_timeout = 45.0 if is_adclarity_url(url) else 30.0
    for candidate in static_candidates or []:
        if candidate == url:
            continue
        data = _try_download_image(client, candidate, timeout=image_timeout)
        if data:
            return save_bytes(thumb_jpg, data), None

    if not opencv_available():
        return None, opencv_unavailable_reason()

    if is_adclarity_url(url):
        ok, probe_detail = _probe_media_url(client, url)
        if not ok:
            print(f"AdClarity probe failed {url[:80]}: {probe_detail}")
            return None, probe_detail

    ext = extension_from_url(url)
    video_timeout = (
        adclarity_video_download_timeout() if is_adclarity_url(url) else 90.0
    )
    last_detail: str | None = None

    partial_path, partial_detail = _fetch_partial_video(
        client, url, cache_dir, key, ext
    )
    if partial_path:
        ok, frame_detail = extract_video_frame(partial_path, thumb_jpg)
        if ok:
            return thumb_jpg, None
        last_detail = frame_detail

    vid_path = cache_dir / f"{key}{ext}"
    if not vid_path.is_file():
        data, dl_detail = _fetch_bytes_sync(client, url, timeout=video_timeout)
        if data:
            save_bytes(vid_path, data)
        else:
            last_detail = dl_detail or last_detail

    if vid_path.is_file():
        ok, frame_detail = extract_video_frame(vid_path, thumb_jpg)
        if ok:
            return thumb_jpg, None
        last_detail = frame_detail

    return (
        None,
        partial_detail or last_detail or "video download failed",
    )


def thumbnail_concurrency() -> int:
    """Parallel download workers (env: YAMI_THUMBNAIL_WORKERS, default 24)."""
    try:
        n = int(os.environ.get("YAMI_THUMBNAIL_WORKERS", str(DEFAULT_THUMBNAIL_WORKERS)))
    except ValueError:
        n = DEFAULT_THUMBNAIL_WORKERS
    return max(1, min(n, 64))


def _http_client() -> httpx.Client:
    limits = httpx.Limits(
        max_connections=32,
        max_keepalive_connections=16,
        keepalive_expiry=30.0,
    )
    return httpx.Client(
        follow_redirects=True,
        timeout=httpx.Timeout(30.0, connect=10.0),
        limits=limits,
    )


def _thread_http_client() -> httpx.Client:
    client = getattr(_thread_local, "http_client", None)
    if client is None:
        client = _http_client()
        _thread_local.http_client = client
    return client


def resolve_thumbnail_with_client(
    url: str,
    cache_dir: Path,
    client: httpx.Client,
) -> tuple[Path | None, str | None]:
    if not url:
        return None, None

    cache_dir = Path(cache_dir)
    key = _cache_key(url)
    cache_dir.mkdir(parents=True, exist_ok=True)

    cached = _existing_cached_thumb(cache_dir, key)
    if cached:
        return cached, None

    ext = extension_from_url(url)
    out = cache_dir / f"{key}{ext}"
    thumb_jpg = cache_dir / f"{key}_thumb.jpg"
    adclarity_static = adclarity_thumbnail_candidates(url) if is_adclarity_url(url) else []

    if is_youtube_url(url):
        yt = _resolve_youtube_thumbnail(client, url, cache_dir, key)
        if yt:
            return yt, None

    if is_tiktok_page_url(url):
        tt = _resolve_tiktok_thumbnail(client, url, cache_dir, key)
        if tt:
            return tt, None

    if is_video_url(url) or ext in VIDEO_EXTENSIONS:
        poster, detail = _resolve_video_poster(
            client, url, cache_dir, key, adclarity_static
        )
        return poster, detail

    if ext in IMAGE_EXTENSIONS and not is_video_url(url):
        if out.is_file() and out.stat().st_size > 0:
            return out, None
        data = _try_download_image(
            client, url, timeout=60.0 if is_adclarity_url(url) else 30.0
        )
        if data:
            return save_bytes(out, data), None
        if adclarity_static:
            poster, detail = _resolve_video_poster(
                client, url, cache_dir, key, adclarity_static
            )
            if poster:
                return poster, None
            if detail:
                return None, detail
        if is_youtube_url(url):
            yt = _resolve_youtube_thumbnail(client, url, cache_dir, key)
            if yt:
                return yt, None
        if is_tiktok_page_url(url):
            tt = _resolve_tiktok_thumbnail(client, url, cache_dir, key)
            if tt:
                return tt, None
        return None, "image download failed or not image bytes"

    data, fetch_detail = _fetch_bytes_sync(
        client, url, timeout=120.0 if is_adclarity_url(url) else 60.0
    )
    if not data:
        if is_youtube_url(url):
            yt = _resolve_youtube_thumbnail(client, url, cache_dir, key)
            return yt, None if yt else fetch_detail
        if is_tiktok_page_url(url):
            tt = _resolve_tiktok_thumbnail(client, url, cache_dir, key)
            return tt, None if tt else fetch_detail
        return None, fetch_detail

    if is_image_bytes(data):
        guessed = extension_from_url(url)
        img_path = cache_dir / f"{key}{guessed}"
        return save_bytes(img_path, data), None

    if thumb_jpg.is_file():
        return thumb_jpg, None
    guessed = extension_from_url(url)
    if guessed in VIDEO_EXTENSIONS or is_video_url(url):
        vid_path = cache_dir / f"{key}{guessed}"
        save_bytes(vid_path, data)
        if adclarity_static:
            poster, detail = _resolve_video_poster(
                client, url, cache_dir, key, adclarity_static
            )
            if poster:
                return poster, None
            if detail:
                return None, detail
        ok, frame_detail = extract_video_frame(vid_path, thumb_jpg)
        if ok:
            return thumb_jpg, None
        return None, frame_detail or "could not extract frame from downloaded video"

    if is_youtube_url(url):
        yt = _resolve_youtube_thumbnail(client, url, cache_dir, key)
        return yt, None if yt else "YouTube thumbnail not found"
    if is_tiktok_page_url(url):
        tt = _resolve_tiktok_thumbnail(client, url, cache_dir, key)
        return tt, None if tt else "TikTok thumbnail not found"
    return None, "unsupported media type"


def resolve_thumbnail_blocking(url: str, cache_dir: Path) -> Path | None:
    """Sync thumbnail resolver for a single URL."""
    with _http_client() as client:
        path, _detail = resolve_thumbnail_with_client(url, cache_dir, client)
        return path


def resolve_thumbnails_batch(
    urls: list[str],
    cache_dir: Path,
    max_workers: int | None = None,
    on_progress=None,
) -> tuple[dict[str, Path | None], dict[str, str]]:
    """Fetch thumbnails in parallel; each distinct URL is downloaded at most once."""
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    unique_urls = list(dict.fromkeys(u for u in urls if u))
    if not unique_urls:
        return {}, {}

    workers = max_workers if max_workers is not None else thumbnail_concurrency()
    workers = max(1, min(workers, len(unique_urls), 64))
    results: dict[str, Path | None] = {}
    failures: dict[str, str] = {}
    downloaded = 0

    def fetch_one(url: str) -> tuple[str, Path | None, str | None]:
        try:
            with _host_request_slot(url):
                client = _thread_http_client()
                path, detail = resolve_thumbnail_with_client(url, cache_dir, client)
                if path and not path.is_file():
                    return url, None, "thumbnail file missing after download"
                return url, path, detail
        except Exception as exc:
            return url, None, str(exc)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_to_url = {
            pool.submit(fetch_one, url): url for url in unique_urls
        }
        done = 0
        for fut in as_completed(future_to_url):
            url = future_to_url[fut]
            try:
                _url, path, detail = fut.result()
            except Exception as exc:
                logger.exception("Thumbnail worker failed for %s", url)
                path, detail = None, str(exc)
            results[url] = path
            if not path and detail:
                failures[url] = detail
                if is_adclarity_url(url):
                    print(f"AdClarity thumb failed {url[:80]}: {detail}")
            done += 1
            if path:
                downloaded += 1
            if on_progress:
                on_progress(done, len(unique_urls), downloaded)

    return results, failures


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
    del client  # single code path in blocking resolver
    return await asyncio.to_thread(resolve_thumbnail_blocking, url, cache_dir)
