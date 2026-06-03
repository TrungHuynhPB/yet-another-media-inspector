"""Map spreadsheet columns to inspect-popup metadata."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

# Primary group title — BRAND / brand
BRAND_COLUMN_CANDIDATES = (
    "BRAND",
    "brand",
    "Brand",
    "vendor_brand",
    "vendorBrand",
    "Vendor_Brand",
    "VENDOR_BRAND",
    "Vendor Brand",
    "VENDOR BRAND",
)

# Used for grouping when no BRAND / VENDOR_BRAND column exists
BRAND_FALLBACK_COLUMN_CANDIDATES = (
    "CREATIVE_CAMPAIGN_NAME",
    "creative_campaign_name",
    "Creative_Campaign_Name",
    "CREATIVE_CAMPAGIN_NAME",  # common spreadsheet typo
    "creative_campagin_name",
)

# Grey subtitle — ADVERTISER_NAME (not used for grouping key)
IS_FAULTY_COLUMN_CANDIDATES = (
    "isFaulty",
    "IS_FAULTY",
    "is_faulty",
    "IsFaulty",
    "isFault",
    "IS_FAULT",
    "isfault",
    "isfaulty",
)

ADVERTISER_NAME_COLUMN_CANDIDATES = (
    "ADVERTISER_NAME",
    "advertiser_name",
    "advertise_name",
    "advertiserName",
    "advertiseName",
    "Advertiser_Name",
    "Advertise_Name",
    "AdvertiserName",
    "advertiser",
    "Advertiser",
)

INSPECT_FIELD_MAP: list[tuple[str, list[str]]] = [
    (
        "brand",
        [
            "brand",
            "BRAND",
            "Brand",
            "vendor_brand",
            "VENDOR_BRAND",
            "Vendor_Brand",
            "CREATIVE_CAMPAIGN_NAME",
            "creative_campaign_name",
        ],
    ),
    (
        "advertiser_name",
        ["ADVERTISER_NAME", "advertiser_name", "advertise_name", "Advertiser_Name"],
    ),
    ("social_description", ["SOCIAL_DESCRIPTION", "social_description"]),
    ("social_headline_text", ["SOCIAL_HEADLINE_TEXT", "social_headline_text"]),
    ("social_campaign_text", ["SOCIAL_CAMPAIGN_TEXT", "social_campaign_text"]),
    ("platform", ["PLATFORM", "platform"]),
    ("creative_campaign_name", ["CREATIVE_CAMPAIGN_NAME", "creative_campaign_name"]),
    ("creative_video_title", ["CREATIVE_VIDEO_TITLE", "creative_video_title"]),
    ("social_page_name", ["SOCIAL_PAGE_NAME", "social_page_name"]),
    ("creative_url", ["CREATIVE_URL_SUPPLIER", "creative_url_supplier", "url", "media_url"]),
]


def _cell_str(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    s = str(value).strip()
    return "" if s.lower() in ("nan", "none") else s


def prepare_upload_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """Strip headers so BRAND / ADVERTISER_NAME match reliably."""
    out = df.copy()
    out.columns = [str(c).strip() for c in out.columns]
    return _repair_legacy_export(out)


def _repair_legacy_export(df: pd.DataFrame) -> pd.DataFrame:
    """Old exports lacked BRAND; promote misnamed advertise_name when appropriate."""
    if detect_brand_column(list(df.columns)):
        return df
    lower = {c.lower(): c for c in df.columns}
    if "advertise_name" not in lower:
        return df
    # Reviewed export from an older session (no BRAND column)
    if "isfault" not in lower and "reviewed" not in lower:
        return df
    return df.rename(columns={lower["advertise_name"]: "BRAND"})


def _column_from_candidates(
    columns: list[str], candidates: tuple[str, ...]
) -> str | None:
    lower = {c.lower(): c for c in columns}
    for cand in candidates:
        if cand.lower() in lower:
            return lower[cand.lower()]
    return None


def detect_brand_column(columns: list[str]) -> str | None:
    col = _column_from_candidates(columns, BRAND_COLUMN_CANDIDATES)
    if col:
        return col
    for col_name in columns:
        normalized = col_name.lower().replace(" ", "_").replace("-", "_")
        if normalized in ("vendor_brand", "vendorbrand"):
            return col_name
        if (
            "vendor" in normalized
            and "brand" in normalized
            and "advertis" not in normalized
        ):
            return col_name
    return _column_from_candidates(columns, BRAND_FALLBACK_COLUMN_CANDIDATES)


def detect_is_faulty_column(columns: list[str]) -> str | None:
    """Optional pre-mark column: TRUE → creative starts with ✕ fault."""
    return _column_from_candidates(columns, IS_FAULTY_COLUMN_CANDIDATES)


def is_faulty_export_column(columns: list[str]) -> str | None:
    """Existing isFaulty column to overwrite on export (not legacy isFault)."""
    lower = {str(c).strip().lower(): c for c in columns}
    for key in ("isfaulty", "is_faulty"):
        if key in lower:
            return lower[key]
    return None


def classified_export_filename(original_filename: str) -> str:
    """e.g. UL_VN_Deodorant_FPK_May26.xlsx → UL_VN_Deodorant_FPK_May26_Classified.xlsx"""
    p = Path((original_filename or "upload.xlsx").strip())
    stem = p.stem or "upload"
    ext = p.suffix if p.suffix else ".xlsx"
    if stem.endswith("_Classified"):
        return f"{stem}{ext}"
    return f"{stem}_Classified{ext}"


def parse_is_faulty_value(value) -> bool:
    """Return True only for explicit truthy spreadsheet values."""
    if value is True:
        return True
    if value is False:
        return False
    if isinstance(value, (int, float)) and not pd.isna(value):
        return value != 0
    s = _cell_str(value).lower()
    if not s:
        return False
    if s in ("true", "1", "1.0", "yes", "y", "t", "x", "✓", "fault", "faulty"):
        return True
    return False


def detect_advertiser_name_column(columns: list[str]) -> str | None:
    """Advertiser name for subtitles only — never the brand grouping column."""
    lower = {c.lower(): c for c in columns}
    brand_col = detect_brand_column(columns)
    for cand in ADVERTISER_NAME_COLUMN_CANDIDATES:
        key = cand.lower()
        if key in lower and lower[key] != brand_col:
            return lower[key]
    for col in columns:
        if col == brand_col:
            continue
        cl = col.lower()
        if cl in ("advertiser_name", "advertise_name", "advertiser"):
            return col
    return None


def brand_column_error(columns: list[str]) -> str:
    return (
        "Brand column not found. Expected BRAND / VENDOR_BRAND, or fallback "
        "CREATIVE_CAMPAIGN_NAME. Advertiser subtitle uses ADVERTISER_NAME. "
        f"Columns: {columns}."
    )


def column_lookup(df: pd.DataFrame) -> dict[str, str | None]:
    lower = {c.lower(): c for c in df.columns}
    out: dict[str, str | None] = {
        "brand": detect_brand_column(list(df.columns)),
        "advertiser_name": detect_advertiser_name_column(list(df.columns)),
    }
    for field, candidates in INSPECT_FIELD_MAP:
        if field in out and out[field]:
            continue
        col = None
        for cand in candidates:
            if cand.lower() in lower:
                col = lower[cand.lower()]
                break
        out[field] = col
    return out


def row_metadata(df: pd.DataFrame, idx: int, lookup: dict[str, str | None]) -> dict[str, str]:
    meta: dict[str, str] = {}
    for field, col in lookup.items():
        if col:
            val = _cell_str(df.iloc[idx][col])
            if val:
                meta[field] = val
    return meta
