"""Find the first Facebook or Instagram creative image URL for a post page."""

import logging
import re
from urllib.parse import urlparse

import httpx
import instaloader
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*",
    "Referer": "https://www.facebook.com/",
}


def is_meta_page_url(url: str) -> bool:
    """Facebook or Instagram post/reel page, not an fbcdn image link."""
    host = urlparse(url or "").netloc.lower()
    if not host or "fbcdn.net" in host:
        return False
    return "instagram.com" in host or "facebook.com" in host


def get_platform(url: str) -> str:
    host = urlparse(url).netloc.lower()
    path = urlparse(url).path.lower()

    if "instagram.com" in host:
        return "instagram"

    if "facebook.com" in host:
        if "/reel/" in path or "/reels/" in path:
            return "facebook_reel"
        return "facebook_post"

    raise ValueError("Unsupported URL")


# ============================================================
# INSTAGRAM: USE INSTALOADER
# ============================================================

def scrape_instagram(url: str) -> list[str]:
    images, _detail = _scrape_instagram(url)
    return images


def _scrape_instagram(url: str) -> tuple[list[str], str | None]:
    match = re.search(
        r"/(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)",
        urlparse(url).path,
    )

    if not match:
        logger.warning("Could not extract Instagram shortcode from %s", url)
        return [], "Instaloader failed"

    shortcode = match.group(1)
    loader = instaloader.Instaloader()

    try:
        post = instaloader.Post.from_shortcode(
            loader.context,
            shortcode,
        )

        images: list[str] = []

        def add(image_url: str | None) -> None:
            if image_url and image_url not in images:
                images.append(image_url)

        # Main image or video thumbnail
        add(post.url)

        # Carousel: extract each slide's media URL
        if post.typename == "GraphSidecar":
            for item in post.get_sidecar_nodes():
                add(item.display_url)

        if images:
            return images, None
        return [], "Instaloader failed"

    except Exception as exc:
        logger.warning(
            "Instaloader failed for %s: %r. The post may require login, "
            "be unavailable, or Instagram may be rate-limiting the request.",
            url,
            exc,
        )
        return [], "Instaloader failed"


# ============================================================
# FACEBOOK: HTML / EMBED EXTRACTION
# ============================================================

def scrape_facebook(
    url: str, client: httpx.Client | None = None
) -> list[str]:
    images, _detail = _scrape_facebook(url, client)
    return images


def _facebook_images_from_html(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    images: list[str] = []

    def add(src: str | None) -> None:
        if not src or not src.startswith("https://"):
            return

        host = urlparse(src).netloc.lower()
        path = urlparse(src).path.lower()

        if "fbcdn.net" not in host:
            return

        # Skip profile pictures
        if "/v/t39.30808-1/" in path:
            return

        if src not in images:
            images.append(src)

    # Prefer actual creative image patterns
    for img in soup.find_all("img"):
        src = img.get("src", "")
        path = urlparse(src).path.lower()

        if "/v/t39.30808-6/" in path or "/v/t15." in path:
            add(src)

    # Fallback to Open Graph image
    if not images:
        for meta in soup.find_all("meta"):
            if meta.get("property") == "og:image":
                add(meta.get("content", ""))

    return images


def _scrape_facebook(
    url: str, client: httpx.Client | None = None
) -> tuple[list[str], str | None]:
    pages = [url]

    if get_platform(url) == "facebook_post":
        pages.append("https://www.facebook.com/plugins/post.php")

    def collect(http: httpx.Client) -> list[str]:
        for page in pages:
            try:
                if "plugins/post.php" in page:
                    response = http.get(
                        page,
                        params={"href": url, "show_text": "true"},
                        headers=HEADERS,
                        timeout=30,
                    )
                else:
                    response = http.get(page, headers=HEADERS, timeout=30)

                if response.status_code != 200:
                    continue

                images = _facebook_images_from_html(response.text)
                if images:
                    return images

            except httpx.HTTPError as exc:
                logger.warning("Facebook request failed for %s: %s", page, exc)

        return []

    if client is not None:
        images = collect(client)
    else:
        with httpx.Client(
            headers=HEADERS,
            follow_redirects=True,
            timeout=30,
        ) as owned:
            images = collect(owned)

    if images:
        return images, None
    return [], "No Facebook/Instagram creative image found"


def first_meta_image_url(
    url: str, client: httpx.Client | None = None
) -> tuple[str | None, str | None]:
    """Return the first scraped image URL, or a short failure detail."""
    try:
        platform = get_platform(url)
    except ValueError:
        return None, "Unsupported URL"

    if platform == "instagram":
        images, detail = _scrape_instagram(url)
    else:
        images, detail = _scrape_facebook(url, client)

    if images:
        return images[0], None
    return None, detail or "No Facebook/Instagram creative image found"
