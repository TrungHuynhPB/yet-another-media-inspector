"""Group creatives by brand (advertiser name) and flag uncertain ads for individual review."""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict

import imagehash
from PIL import Image

# Perceptual-hash distance above this (0–64) suggests a different visual identity.
OUTLIER_MIN_DISTANCE = 18


def normalize_brand_key(name: str) -> str:
    return name.strip().lower() if name else ""


def brand_display_name(names: list[str]) -> str:
    cleaned = [n.strip() for n in names if n and n.strip()]
    if not cleaned:
        return "Unknown brand"
    return Counter(cleaned).most_common(1)[0][0]


def _phash(path: str) -> imagehash.ImageHash | None:
    try:
        with Image.open(path) as img:
            return imagehash.phash(img)
    except Exception:
        return None


def find_visual_outliers(members: list[dict]) -> list[dict]:
    """
    Within a brand, flag creatives that look unlike the rest (possible mis-label).
    Requires at least 4 thumbnails in the brand.
    """
    with_thumb = [m for m in members if m.get("thumb")]
    if len(with_thumb) < 4:
        return []

    hashes: dict[int, imagehash.ImageHash] = {}
    for m in with_thumb:
        h = _phash(m["thumb"])
        if h is not None:
            hashes[int(m["index"])] = h

    if len(hashes) < 4:
        return []

    avg_distances: list[tuple[float, dict]] = []
    for m in with_thumb:
        idx = int(m["index"])
        if idx not in hashes:
            continue
        h = hashes[idx]
        others = [hashes[j] for j in hashes if j != idx]
        avg = sum(h - o for o in others) / len(others)
        avg_distances.append((avg, m))

    if not avg_distances:
        return []

    values = [d for d, _ in avg_distances]
    mean = statistics.mean(values)
    stdev = statistics.stdev(values) if len(values) > 1 else 0.0
    cutoff = max(OUTLIER_MIN_DISTANCE, mean + 1.25 * stdev)

    return [m for d, m in avg_distances if d > cutoff]


def build_brand_groups(
    rows: list[dict],
    to_items,
) -> tuple[list[dict], list[dict]]:
    """
    Returns (brand_groups, uncertain_items).
    Brand groups are keyed by advertiser name from the spreadsheet.
    Uncertain items need one-by-one advertiser verification.
    """
    by_brand: dict[str, list[dict]] = defaultdict(list)
    uncertain_rows: list[dict] = []

    for row in rows:
        if not row.get("url"):
            continue
        key = normalize_brand_key(row.get("advertiserName") or "")
        if not key:
            row["uncertainReason"] = "missing_advertiser"
            uncertain_rows.append(row)
        else:
            by_brand[key].append(row)

    brand_groups: list[dict] = []
    gid = 0

    for _key in sorted(by_brand.keys(), key=lambda k: brand_display_name([m["advertiserName"] for m in by_brand[k]])):
        members = by_brand[_key]
        outliers = find_visual_outliers(members)
        outlier_ids = {int(m["index"]) for m in outliers}

        for m in outliers:
            m["uncertainReason"] = "visual_outlier"
            m["groupId"] = None
            uncertain_rows.append(m)

        brand_members = [m for m in members if int(m["index"]) not in outlier_ids]
        if not brand_members:
            continue

        title = brand_display_name([m["advertiserName"] for m in brand_members])
        for m in brand_members:
            m["groupId"] = gid

        items = to_items(brand_members)
        brand_groups.append(
            {
                "groupId": gid,
                "type": "brand",
                "title": title,
                "memberIndices": [int(m["index"]) for m in brand_members],
                "items": items,
                "thumbs": [i["thumbUrl"] for i in items if i.get("thumbUrl")],
                "count": len(brand_members),
                "urls": [m["url"] for m in brand_members],
            }
        )
        gid += 1

    uncertain_items: list[dict] = []
    for item_id, row in enumerate(uncertain_rows):
        row["needsIndividualReview"] = True
        items = to_items([row])
        uncertain_items.append(
            {
                "itemId": item_id,
                "rowIndex": int(row["index"]),
                "type": "individual",
                "title": row.get("advertiserName") or "Unknown advertiser",
                "uncertainReason": row.get("uncertainReason", "review"),
                "items": items,
                "count": 1,
            }
        )

    return brand_groups, uncertain_items
