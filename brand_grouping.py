"""Group creatives by brand column; advertiser_name shown as subtitle in UI."""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from pathlib import Path

import imagehash
from PIL import Image

from grouping import KMeansGrouper, optimal_k
from media import is_video_url

OUTLIER_MIN_DISTANCE = 18
MIN_VISUAL_SUBGROUP_SIZE = 2
MIN_ITEMS_FOR_VISUAL_SUBGROUPING = 6


def normalize_brand_key(name: str) -> str:
    return name.strip().lower() if name else ""


def display_name(names: list[str], fallback: str = "Unknown") -> str:
    cleaned = [n.strip() for n in names if n and n.strip()]
    if not cleaned:
        return fallback
    return Counter(cleaned).most_common(1)[0][0]


def _phash(path: str) -> imagehash.ImageHash | None:
    try:
        with Image.open(path) as img:
            return imagehash.phash(img)
    except Exception:
        return None


def find_visual_outliers(members: list[dict]) -> list[dict]:
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


def unavailable_media_reason(url: str) -> str:
    return "Unavailable video" if is_video_url(url or "") else "Unavailable image"


def _uncertain_reason_hint(reasons: set[str]) -> str:
    labels = {
        "visual_outlier": "Creatives look different from others in this brand.",
        "missing_brand": "No brand label on these rows.",
        "visual_singleton": "Creatives did not match a visual subgroup.",
    }
    if len(reasons) == 1:
        return labels.get(next(iter(reasons)), "Verify brand labels for this group.")
    return "Verify brand labels for this group."


def _build_uncertain_groups(
    uncertain_rows: list[dict],
    to_items,
) -> list[dict]:
    """Group uncertain rows by brand so reviewers swipe per brand, not per creative."""
    eligible = [
        row
        for row in uncertain_rows
        if row.get("url") and (row.get("thumb") or row.get("thumbRemote"))
    ]
    by_brand: dict[str, list[dict]] = defaultdict(list)
    for row in eligible:
        key = normalize_brand_key(row.get("brandName") or "") or "__unknown__"
        by_brand[key].append(row)

    groups: list[dict] = []
    sorted_keys = sorted(
        by_brand.keys(),
        key=lambda k: display_name(
            [m["brandName"] for m in by_brand[k]], "Unknown brand"
        ),
    )
    for group_id, key in enumerate(sorted_keys):
        members = by_brand[key]
        title = display_name([m["brandName"] for m in members], "Unknown brand")
        subtitle = display_name(
            [m.get("advertiserName") or "" for m in members],
            "",
        )
        reasons = {m.get("uncertainReason", "review") for m in members}
        for m in members:
            m["needsIndividualReview"] = True

        items = to_items(members)
        groups.append(
            {
                "groupId": group_id,
                "type": "uncertain",
                "title": title,
                "subtitle": subtitle,
                "uncertainReason": next(iter(reasons)) if len(reasons) == 1 else "mixed",
                "reasonHint": _uncertain_reason_hint(reasons),
                "memberIndices": [int(m["index"]) for m in members],
                "items": items,
                "count": len(members),
            }
        )
    return groups


def build_unavailable_media(rows: list[dict]) -> dict | None:
    entries = []
    for row in rows:
        if row.get("thumb") or row.get("thumbRemote") or not row.get("url"):
            continue
        url = row["url"]
        fetch_detail = (row.get("thumbFetchDetail") or "").strip()
        reason = unavailable_media_reason(url)
        if fetch_detail:
            reason = f"{reason} — {fetch_detail}"
        entries.append(
            {
                "rowIndex": int(row["index"]),
                "brand": row.get("brandName") or "",
                "advertiserName": row.get("advertiserName") or "",
                "creativeUrl": url,
                "reason": reason,
                "fetchDetail": fetch_detail,
            }
        )
    if not entries:
        return None
    return {
        "type": "unavailable",
        "title": "Unavailable media",
        "count": len(entries),
        "entries": entries,
    }


def build_brand_groups(
    rows: list[dict],
    to_items,
) -> tuple[list[dict], list[dict], dict | None]:
    by_brand: dict[str, list[dict]] = defaultdict(list)
    uncertain_rows: list[dict] = []

    for row in rows:
        if not row.get("url"):
            continue
        key = normalize_brand_key(row.get("brandName") or "")
        if not key:
            row["uncertainReason"] = "missing_brand"
            uncertain_rows.append(row)
        else:
            by_brand[key].append(row)

    brand_groups: list[dict] = []
    gid = 0

    for _key in sorted(
        by_brand.keys(),
        key=lambda k: display_name([m["brandName"] for m in by_brand[k]], "Unknown brand"),
    ):
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

        # Optional second pass: within a single brand, split into multiple visual subgroups
        # so colors/themes end up in separate review grids.
        brand_title = display_name([m["brandName"] for m in brand_members], "Unknown brand")
        brand_subtitle = display_name([m.get("advertiserName") or "" for m in brand_members], "")

        for m in brand_members:
            thumb = m.get("thumb")
            if thumb and not Path(thumb).is_file():
                m["thumb"] = None
        with_thumb_local = [m for m in brand_members if m.get("thumb")]
        for m in brand_members:
            if not m.get("thumb") and not m.get("thumbRemote"):
                m["uncertainReason"] = "missing_thumb"
                m["groupId"] = None

        subgroups: list[list[dict]] = []
        if len(with_thumb_local) >= MIN_ITEMS_FOR_VISUAL_SUBGROUPING:
            with_thumb_valid = [
                m for m in with_thumb_local if Path(m["thumb"]).is_file()
            ]
            thumb_paths = [m["thumb"] for m in with_thumb_valid]
            k = optimal_k(len(thumb_paths))
            grouper = KMeansGrouper(k=k, resample=128)
            cluster_ids = grouper.fit(thumb_paths, show_progress=False)
            by_cluster: dict[int, list[dict]] = defaultdict(list)
            for m, cid in zip(with_thumb_valid, cluster_ids):
                by_cluster[int(cid)].append(m)

            # Keep meaningful subgroups; push singletons to individual review.
            for cid, ms in sorted(by_cluster.items(), key=lambda kv: (-len(kv[1]), kv[0])):
                if len(ms) < MIN_VISUAL_SUBGROUP_SIZE:
                    for m in ms:
                        m["uncertainReason"] = "visual_singleton"
                        m["groupId"] = None
                        uncertain_rows.append(m)
                else:
                    subgroups.append(ms)
        else:
            groupable = [
                m
                for m in brand_members
                if m.get("thumb") or m.get("thumbRemote")
            ]
            subgroups = [groupable] if groupable else []

        grouped_ids = {int(m["index"]) for sg in subgroups for m in sg}
        remote_or_unclustered = [
            m
            for m in brand_members
            if int(m["index"]) not in grouped_ids
            and (m.get("thumbRemote") or m.get("thumb"))
            and not m.get("uncertainReason")
        ]
        if remote_or_unclustered:
            if subgroups:
                subgroups[0].extend(remote_or_unclustered)
            else:
                subgroups = [remote_or_unclustered]

        n_groups = len(subgroups)
        for sg_i, members_in_group in enumerate(subgroups):
            title = (
                f"{brand_title} ({sg_i + 1}/{n_groups})"
                if n_groups > 1
                else brand_title
            )
            subtitle = brand_subtitle

            for m in members_in_group:
                m["groupId"] = gid

            items = to_items(members_in_group)
            brand_groups.append(
                {
                    "groupId": gid,
                    "type": "brand",
                    "title": title,
                    "subtitle": subtitle,
                    "memberIndices": [int(m["index"]) for m in members_in_group],
                    "items": items,
                    "thumbs": [i["thumbUrl"] for i in items if i.get("thumbUrl")],
                    "count": len(members_in_group),
                    "urls": [m["url"] for m in members_in_group],
                }
            )
            gid += 1

    uncertain_items = _build_uncertain_groups(uncertain_rows, to_items)

    unavailable_media = build_unavailable_media(rows)

    return brand_groups, uncertain_items, unavailable_media
