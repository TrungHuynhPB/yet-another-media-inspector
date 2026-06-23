"""Media Inspector — group creatives by brand and swipe-review for faults."""

import asyncio
import json
import logging
import os
import pickle
import sys
import uuid
from collections.abc import AsyncIterator
from io import BytesIO, StringIO
from pathlib import Path

import httpx
import pandas as pd
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles

from brand_grouping import build_brand_groups
from hints_loader import hint_rotate_ms, load_hints
from metadata import column_lookup, row_metadata
from metadata import (
    brand_column_error,
    classified_export_filename,
    detect_advertiser_name_column,
    detect_brand_column,
    detect_is_faulty_column,
    is_faulty_export_column,
    parse_is_faulty_value,
    prepare_upload_dataframe,
)
from media import (
    detect_url_column,
    is_youtube_url,
    opencv_available,
    opencv_diagnostics,
    opencv_unavailable_reason,
    resolve_thumbnails_batch,
    thumbnail_concurrency,
    youtube_video_id,
)

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"


def _is_serverless() -> bool:
    return bool(
        os.environ.get("VERCEL")
        or os.environ.get("VERCEL_ENV")
        or os.environ.get("AWS_LAMBDA_FUNCTION_NAME")
        or os.environ.get("LAMBDA_TASK_ROOT")
    )


def _default_data_dir() -> Path:
    if _is_serverless():
        return Path("/tmp/yami-data")
    return BASE_DIR / "data"


DATA_DIR = Path(os.environ.get("YAMI_DATA_DIR", str(_default_data_dir())))
JOBS_DIR = DATA_DIR / "jobs"
SESSIONS_DIR = DATA_DIR / "sessions"

_VERCEL_BLOB_API_BASE_URL = "https://blob.vercel-storage.com"
_BLOB_API_VERSION = "10"
_blob_index: dict[str, dict] = {}

# Vercel serverless rejects request bodies above ~4.5 MB (413) before the app runs.
VERCEL_MAX_UPLOAD_BYTES = 4_500_000
MAX_UPLOAD_BYTES = int(
    os.environ.get("YAMI_MAX_UPLOAD_BYTES", str(VERCEL_MAX_UPLOAD_BYTES))
)


def _upload_too_large_detail(size: int) -> str:
    mb = VERCEL_MAX_UPLOAD_BYTES / (1024 * 1024)
    return (
        f"Upload is {size / (1024 * 1024):.1f} MB — max about {mb:.1f} MB on Vercel. "
        "Remove unused columns, save a smaller Excel/CSV, or split the file."
    )


def _reject_oversized_upload(raw: bytes) -> None:
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, _upload_too_large_detail(len(raw)))


def _blob_enabled() -> bool:
    # Uses the Blob REST API directly (no SDK dependency).
    return bool(_is_serverless() and os.environ.get("BLOB_READ_WRITE_TOKEN"))


def _blob_thumbs_enabled() -> bool:
    """Per-thumbnail Blob uploads are opt-in — each PUT counts as an advanced op."""
    if not _blob_enabled():
        return False
    return os.environ.get("YAMI_BLOB_THUMBS", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def _blob_session_enabled() -> bool:
    """Persist sessions to Blob (pickle, review, source). Off by default — review is client-side."""
    if not _blob_enabled():
        return False
    return os.environ.get("YAMI_BLOB_SESSION", "0").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def _blob_headers() -> dict[str, str]:
    return {
        "authorization": f"Bearer {os.environ.get('BLOB_READ_WRITE_TOKEN','')}",
        "x-api-version": _BLOB_API_VERSION,
        "access": "public",
    }


def _blob_list(prefix: str | None = None, *, limit: int = 1000) -> list[dict]:
    if not _blob_enabled():
        return []
    params: dict[str, str] = {"limit": str(limit)}
    if prefix:
        params["prefix"] = prefix
    try:
        resp = httpx.get(
            _VERCEL_BLOB_API_BASE_URL,
            params=params,
            headers=_blob_headers(),
            timeout=15.0,
            follow_redirects=True,
        )
        resp.raise_for_status()
        data = resp.json()
        return list(data.get("blobs") or [])
    except Exception as exc:
        logger.warning("Blob list failed: %s", exc)
        return []


def _blob_find(pathname: str) -> dict | None:
    cached = _blob_index.get(pathname)
    if cached:
        return cached
    if not _blob_enabled():
        return None
    # Try listing only within the folder for speed.
    prefix = pathname.split("/", 1)[0] + "/" if "/" in pathname else None
    for b in _blob_list(prefix=prefix, limit=1000):
        if b.get("pathname") == pathname:
            _blob_index[pathname] = b
            return b
    return None


def _blob_get_bytes(pathname: str, *, timeout: float = 20.0) -> bytes | None:
    blob = _blob_find(pathname)
    if not blob:
        return None
    url = blob.get("downloadUrl") or blob.get("url")
    if not url:
        return None
    try:
        r = httpx.get(url, timeout=timeout, follow_redirects=True)
        r.raise_for_status()
        return r.content
    except Exception as exc:
        logger.warning("Blob download failed %s: %s", pathname, exc)
        return None


def _blob_put_bytes(pathname: str, data: bytes, *, content_type: str) -> str | None:
    if not _blob_enabled():
        return None
    try:
        # Vercel Blob upload uses PUT /?pathname=...
        resp = httpx.put(
            f"{_VERCEL_BLOB_API_BASE_URL}/",
            params={"pathname": pathname},
            headers={
                **_blob_headers(),
                "x-content-type": content_type,
                "x-cache-control-max-age": "31536000",
                "x-add-random-suffix": "0",
                "x-allow-overwrite": "1",
            },
            content=data,
            timeout=20.0,
            follow_redirects=True,
        )
        resp.raise_for_status()
        info = resp.json()
        if isinstance(info, dict) and info.get("pathname"):
            _blob_index[info["pathname"]] = info
            return info.get("downloadUrl") or info.get("url")
    except Exception as exc:
        logger.warning("Blob put failed %s: %s", pathname, exc)
    return None


def _ensure_data_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="YAMI Media Inspector")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

def _opencv_warning() -> str:
    if opencv_available():
        return ""
    return opencv_unavailable_reason() or (
        f"opencv-python is not installed for {sys.executable} — "
        "video creatives may show as unavailable."
    )


@app.on_event("startup")
async def _check_opencv_on_startup() -> None:
    _ensure_data_dirs()
    if _is_serverless():
        logger.info("Serverless mode: DATA_DIR=%s", DATA_DIR)
        os.environ.setdefault("YAMI_THUMBNAIL_WORKERS", "12")
        os.environ.setdefault("YAMI_ADCLARITY_MAX_PARALLEL", "3")
    diag = opencv_diagnostics()
    logger.info("Python: %s", diag["pythonExecutable"])
    if diag.get("opencvAvailable"):
        logger.info("OpenCV %s ready", diag.get("opencvVersion", ""))
    else:
        logger.warning(_opencv_warning())


def _opencv_warnings() -> list[str]:
    msg = _opencv_warning()
    return [] if not msg else [msg]


_sessions: dict[str, dict] = {}
_jobs: dict[str, dict] = {}


def _review_path(session_id: str) -> Path:
    return SESSIONS_DIR / f"{session_id}.review.json"


def _export_checkpoint_path(session_id: str) -> Path:
    return _session_dir(session_id) / "export_checkpoint.xlsx"


def _review_snapshot(sess: dict) -> dict:
    return {
        "url_column": sess.get("url_column"),
        "brand_column": sess.get("brand_column"),
        "adv_column": sess.get("adv_column"),
        "rows": {
            str(r["index"]): {
                "isFault": bool(r.get("isFault")),
                "faultManual": bool(r.get("faultManual")),
                "advertiserMatch": r.get("advertiserMatch"),
                "reviewed": bool(r.get("reviewed")),
                "brandName": r.get("brandName", ""),
                "advertiserName": r.get("advertiserName", ""),
            }
            for r in sess.get("rows", [])
        },
    }


def _merge_review_snapshot(sess: dict, snapshot: dict) -> None:
    rows = snapshot.get("rows") or snapshot.get("faults") or {}
    for row in sess.get("rows", []):
        state = rows.get(str(row["index"]))
        if state is None:
            continue
        row["isFault"] = bool(state.get("isFault"))
        row["faultManual"] = bool(state.get("faultManual"))
        if "advertiserMatch" in state:
            row["advertiserMatch"] = state.get("advertiserMatch")
        if "reviewed" in state:
            row["reviewed"] = bool(state.get("reviewed"))
        if state.get("brandName"):
            row["brandName"] = state["brandName"]
        if state.get("advertiserName"):
            row["advertiserName"] = state["advertiserName"]


def _write_review_snapshot(
    session_id: str, sess: dict, *, to_blob: bool = False
) -> None:
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(_review_snapshot(sess)).encode("utf-8")
    _review_path(session_id).write_bytes(payload)
    if to_blob and _blob_session_enabled():
        _blob_put_bytes(
            f"sessions/{session_id}.review.json",
            payload,
            content_type="application/json",
        )


def _load_review_snapshot(session_id: str) -> dict | None:
    path = _review_path(session_id)
    if not path.is_file() and _blob_session_enabled():
        data = _blob_get_bytes(f"sessions/{session_id}.review.json", timeout=20.0)
        if data:
            _ensure_data_dirs()
            path.write_bytes(data)
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Failed to read review snapshot %s: %s", session_id, exc)
    # Legacy faults-only snapshot
    legacy = SESSIONS_DIR / f"{session_id}.faults.json"
    if not legacy.is_file() and _blob_session_enabled():
        data = _blob_get_bytes(f"sessions/{session_id}.faults.json", timeout=15.0)
        if data:
            _ensure_data_dirs()
            legacy.write_bytes(data)
    if legacy.is_file():
        try:
            return json.loads(legacy.read_text(encoding="utf-8"))
        except Exception:
            pass
    return None


def _source_blob_paths(session_id: str, ext: str) -> str:
    return f"sessions/{session_id}/source{ext}"


def _persist_upload_source(session_id: str, raw: bytes, filename: str) -> None:
    ext = Path(filename or "upload.xlsx").suffix.lower() or ".xlsx"
    if ext not in (".xlsx", ".xls", ".csv", ".json"):
        ext = ".xlsx"
    out_dir = _session_dir(session_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"source{ext}"
    path.write_bytes(raw)
    if _blob_session_enabled():
        _blob_put_bytes(
            _source_blob_paths(session_id, ext),
            raw,
            content_type="application/octet-stream",
        )
    meta = json.dumps({"filename": filename or "upload.xlsx"}).encode("utf-8")
    (out_dir / "source_meta.json").write_bytes(meta)
    if _blob_session_enabled():
        _blob_put_bytes(
            f"sessions/{session_id}/source_meta.json",
            meta,
            content_type="application/json",
        )


def _thumb_blob_pathname(session_id: str, filename: str) -> str:
    return f"sessions/{session_id}/thumbs/{filename}"


def _persist_thumb_to_blob(session_id: str, thumb_path: Path) -> str | None:
    if not thumb_path.is_file() or not _blob_enabled():
        return None
    data = thumb_path.read_bytes()
    url = _blob_put_bytes(
        _thumb_blob_pathname(session_id, thumb_path.name),
        data,
        content_type="image/jpeg",
    )
    try:
        thumb_path.unlink()
    except OSError as exc:
        logger.warning("Could not delete local thumb %s: %s", thumb_path, exc)
    return url


def _purge_session_cache(session_id: str) -> None:
    cache_dir = _session_dir(session_id) / "cache"
    if not cache_dir.is_dir():
        return
    for path in cache_dir.iterdir():
        try:
            if path.is_file():
                path.unlink()
        except OSError as exc:
            logger.warning("Could not delete cache file %s: %s", path, exc)


def _finalize_session_thumbs(session_id: str, rows: list[dict]) -> None:
    """Free /tmp after grouping; optional Blob offload (off by default on Vercel)."""
    if _blob_thumbs_enabled():
        for row in rows:
            thumb = row.get("thumb")
            if not thumb or row.get("thumbRemote"):
                continue
            path = Path(thumb)
            if not path.is_file():
                row["thumb"] = None
                continue
            url = _persist_thumb_to_blob(session_id, path)
            if url:
                row["thumbRemote"] = url
                row["thumb"] = None
    else:
        for row in rows:
            if row.get("thumb") and not row.get("thumbRemote"):
                row["thumb"] = None
    _purge_session_cache(session_id)


def _row_has_thumb(row: dict) -> bool:
    return bool(row.get("thumb") or row.get("thumbRemote"))


def _load_source_filename(session_id: str, sess: dict | None = None) -> str:
    if sess and sess.get("sourceFilename"):
        return str(sess["sourceFilename"])
    path = _session_dir(session_id) / "source_meta.json"
    if not path.is_file() and _blob_session_enabled():
        data = _blob_get_bytes(f"sessions/{session_id}/source_meta.json", timeout=10.0)
        if data:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8")).get("filename") or ""
        except Exception:
            pass
    return ""


def _export_download_name(session_id: str, sess: dict | None = None) -> str:
    return classified_export_filename(_load_source_filename(session_id, sess))


def _load_source_bytes(session_id: str) -> tuple[bytes, str] | None:
    for ext in (".xlsx", ".xls", ".csv", ".json"):
        path = _session_dir(session_id) / f"source{ext}"
        if path.is_file():
            return path.read_bytes(), f"source{ext}"
        if _blob_session_enabled():
            data = _blob_get_bytes(_source_blob_paths(session_id, ext), timeout=30.0)
            if data:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                return data, f"source{ext}"
    return None


def _build_export_dataframe(sess: dict) -> pd.DataFrame:
    df = sess["df"].copy()
    rows_by_idx = {int(r["index"]): r for r in sess["rows"]}
    df["BRAND"] = [rows_by_idx.get(i, {}).get("brandName", "") for i in range(len(df))]
    df["ADVERTISER_NAME"] = [
        rows_by_idx.get(i, {}).get("advertiserName", "") for i in range(len(df))
    ]
    faulty_values = [
        bool(rows_by_idx.get(i, {}).get("isFault", False)) for i in range(len(df))
    ]
    faulty_col = is_faulty_export_column(list(df.columns))
    if faulty_col:
        df[faulty_col] = faulty_values
    else:
        df["isFaulty"] = faulty_values
    drop_legacy = [c for c in df.columns if str(c).strip().lower() == "isfault"]
    if drop_legacy:
        df = df.drop(columns=drop_legacy)
    df["advertiserMatch"] = [rows_by_idx.get(i, {}).get("advertiserMatch") for i in range(len(df))]
    df["reviewed"] = [rows_by_idx.get(i, {}).get("reviewed", False) for i in range(len(df))]
    return df


def _build_export_bytes(sess: dict) -> bytes:
    buf = BytesIO()
    _build_export_dataframe(sess).to_excel(buf, index=False)
    return buf.getvalue()


def _build_export_from_artifacts(session_id: str) -> bytes | None:
    """Rebuild export when session pickle is missing but source + review snapshot exist."""
    source = _load_source_bytes(session_id)
    review = _load_review_snapshot(session_id)
    if not source or not review:
        return None
    raw, name = source
    df = _load_dataframe(raw, name)
    rows = review.get("rows") or {}
    brand_names = [rows.get(str(i), {}).get("brandName", "") for i in range(len(df))]
    adv_names = [rows.get(str(i), {}).get("advertiserName", "") for i in range(len(df))]
    df["BRAND"] = brand_names
    df["ADVERTISER_NAME"] = adv_names
    faulty_values = [
        bool(rows.get(str(i), {}).get("isFault", False)) for i in range(len(df))
    ]
    faulty_col = is_faulty_export_column(list(df.columns))
    if faulty_col:
        df[faulty_col] = faulty_values
    else:
        df["isFaulty"] = faulty_values
    drop_legacy = [c for c in df.columns if str(c).strip().lower() == "isfault"]
    if drop_legacy:
        df = df.drop(columns=drop_legacy)
    df["advertiserMatch"] = [rows.get(str(i), {}).get("advertiserMatch") for i in range(len(df))]
    df["reviewed"] = [bool(rows.get(str(i), {}).get("reviewed", False)) for i in range(len(df))]
    buf = BytesIO()
    df.to_excel(buf, index=False)
    return buf.getvalue()


def _build_export_from_client_review(session_id: str, review_rows: dict) -> bytes | None:
    """Build export from uploaded source + browser review snapshot (Tier B)."""
    source = _load_source_bytes(session_id)
    if not source:
        return None
    raw, name = source
    df = _load_dataframe(raw, name)
    rows = review_rows or {}
    brand_names = [rows.get(str(i), {}).get("brandName", "") for i in range(len(df))]
    adv_names = [rows.get(str(i), {}).get("advertiserName", "") for i in range(len(df))]
    df["BRAND"] = brand_names
    df["ADVERTISER_NAME"] = adv_names
    faulty_values = [
        bool(rows.get(str(i), {}).get("isFault", False)) for i in range(len(df))
    ]
    faulty_col = is_faulty_export_column(list(df.columns))
    if faulty_col:
        df[faulty_col] = faulty_values
    else:
        df["isFaulty"] = faulty_values
    drop_legacy = [c for c in df.columns if str(c).strip().lower() == "isfault"]
    if drop_legacy:
        df = df.drop(columns=drop_legacy)
    df["advertiserMatch"] = [rows.get(str(i), {}).get("advertiserMatch") for i in range(len(df))]
    df["reviewed"] = [bool(rows.get(str(i), {}).get("reviewed", False)) for i in range(len(df))]
    buf = BytesIO()
    df.to_excel(buf, index=False)
    return buf.getvalue()


def _checkpoint_export(session_id: str, sess: dict, *, to_blob: bool = False) -> None:
    try:
        data = _build_export_bytes(sess)
        path = _export_checkpoint_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        if to_blob and _blob_session_enabled():
            _blob_put_bytes(
                f"sessions/{session_id}/export.xlsx",
                data,
                content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
    except Exception as exc:
        logger.warning("Export checkpoint failed for %s: %s", session_id, exc)


def _load_export_checkpoint(session_id: str) -> bytes | None:
    path = _export_checkpoint_path(session_id)
    if path.is_file() and path.stat().st_size > 0:
        return path.read_bytes()
    if _blob_session_enabled():
        data = _blob_get_bytes(
            f"sessions/{session_id}/export.xlsx",
            timeout=30.0,
        )
        if data:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            return data
    return None


def _export_file_response(data: bytes, filename: str = "media_inspector_output.xlsx"):
    return StreamingResponse(
        BytesIO(data),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _session_get(session_id: str) -> dict | None:
    if session_id in _sessions:
        sess = _sessions[session_id]
    else:
        path = SESSIONS_DIR / f"{session_id}.pkl"
        if not path.is_file() and _blob_session_enabled():
            data = _blob_get_bytes(f"sessions/{session_id}.pkl", timeout=25.0)
            if data:
                _ensure_data_dirs()
                path.write_bytes(data)
        if not path.is_file():
            logger.info(
                "session_get: missing session_id=%s blobEnabled=%s",
                session_id,
                _blob_enabled(),
            )
            return None
        try:
            sess = pickle.loads(path.read_bytes())
            _sessions[session_id] = sess
        except Exception as exc:
            logger.exception("Failed to load session %s: %s", session_id, exc)
            return None

    if _is_serverless():
        snapshot = _load_review_snapshot(session_id)
        if snapshot:
            try:
                _merge_review_snapshot(sess, snapshot)
            except Exception as exc:
                logger.warning("Failed to merge review snapshot for %s: %s", session_id, exc)
    return sess


def _session_put(session_id: str, sess: dict, *, full: bool = True) -> None:
    """Persist session state.

    Review progress is stored in the browser (Tier B). Server keeps /tmp pickle for
    export source merge. Blob puts only when YAMI_BLOB_SESSION=1.
    """
    _sessions[session_id] = sess
    if not _is_serverless():
        if full:
            try:
                _checkpoint_export(session_id, sess, to_blob=False)
            except Exception:
                pass
        return
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    if full:
        path = SESSIONS_DIR / f"{session_id}.pkl"
        data = pickle.dumps(sess, protocol=pickle.HIGHEST_PROTOCOL)
        path.write_bytes(data)
        if _blob_session_enabled():
            _blob_put_bytes(
                f"sessions/{session_id}.pkl", data, content_type="application/octet-stream"
            )
    _write_review_snapshot(session_id, sess, to_blob=full and _blob_session_enabled())
    if full:
        try:
            _checkpoint_export(session_id, sess, to_blob=False)
        except Exception:
            pass


def _job_get(job_id: str) -> dict | None:
    if job_id in _jobs:
        return _jobs[job_id]
    path = JOBS_DIR / f"{job_id}.json"
    if path.is_file():
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
            _jobs[job_id] = job
            return job
        except Exception as exc:
            logger.exception("Failed to load job %s: %s", job_id, exc)
    return None


def _job_set(job_id: str, job: dict) -> None:
    _jobs[job_id] = job
    if _is_serverless():
        JOBS_DIR.mkdir(parents=True, exist_ok=True)
        path = JOBS_DIR / f"{job_id}.json"
        path.write_text(json.dumps(job, default=str), encoding="utf-8")


def _session_dir(session_id: str) -> Path:
    return DATA_DIR / session_id


def _validated_thumb(
    thumb: Path | None, url: str, fetch_failures: dict[str, str]
) -> Path | None:
    if not thumb:
        return None
    path = Path(thumb)
    if path.is_file() and path.stat().st_size > 0:
        return path
    fetch_failures.setdefault(url, "thumbnail file missing after download")
    return None


def _cell_str(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    s = str(value).strip()
    return "" if s.lower() in ("nan", "none") else s


def _thumb_url(session_id: str, thumb_path: str) -> str:
    return f"/api/thumb/{session_id}/{Path(thumb_path).name}"

def _youtube_ui_thumb(url: str) -> str | None:
    vid = youtube_video_id(url)
    if not vid:
        return None
    # 320x180 is a good balance for the grid.
    return f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg"


def _member_items(session_id: str, members: list[dict]) -> list[dict]:
    items = []
    for m in members:
        if not m.get("url"):
            continue
        item = {
            "rowIndex": int(m["index"]),
            "mediaUrl": m["url"],
            "isFault": bool(m.get("isFault", False)),
            "metadata": m.get("metadata") or {},
        }
        if m.get("thumbRemote"):
            item["thumbUrl"] = m["thumbRemote"]
        elif m.get("thumb"):
            item["thumbUrl"] = _thumb_url(session_id, m["thumb"])
        elif is_youtube_url(m["url"]):
            yt = _youtube_ui_thumb(m["url"])
            if yt:
                item["thumbUrl"] = yt
        items.append(item)
    return items


def _read_csv_bytes(raw: bytes) -> pd.DataFrame:
    """Parse CSV with common encodings and auto-detected delimiter (; or ,)."""
    last_err: Exception | None = None
    for encoding in ("utf-8-sig", "utf-8", "latin-1", "cp1252"):
        try:
            text = raw.decode(encoding)
            return pd.read_csv(StringIO(text), sep=None, engine="python")
        except Exception as exc:
            last_err = exc
    raise HTTPException(400, f"Could not read CSV: {last_err}")


def _load_dataframe(raw: bytes, filename: str) -> pd.DataFrame:
    name = (filename or "").lower()
    if name.endswith(".json"):
        payload = json.loads(raw.decode("utf-8"))
        if isinstance(payload, list):
            return prepare_upload_dataframe(pd.DataFrame(payload))
        return prepare_upload_dataframe(
            pd.DataFrame(payload.get("rows", payload.get("data", [payload])))
        )
    if name.endswith((".xlsx", ".xls")):
        return prepare_upload_dataframe(pd.read_excel(BytesIO(raw)))
    if name.endswith(".csv"):
        return prepare_upload_dataframe(_read_csv_bytes(raw))
    if name.endswith(".txt"):
        return prepare_upload_dataframe(_read_csv_bytes(raw))
    raise HTTPException(400, "Upload .xlsx, .xls, .csv, or .json")


def _default_url_column(columns: list[str]) -> str | None:
    lower = {c.lower(): c for c in columns}
    if "creative_url_supplier" in lower:
        return lower["creative_url_supplier"]
    return detect_url_column(columns)


def _default_brand_column(columns: list[str]) -> str | None:
    lower = {c.lower(): c for c in columns}
    if "brand" in lower:
        return lower["brand"]
    return detect_brand_column(columns)


def _resolve_columns(
    df: pd.DataFrame, url_column: str, brand_column: str = ""
) -> tuple[str, str, str | None]:
    col = url_column.strip() if url_column else None
    if not col:
        col = _default_url_column(list(df.columns))
    if not col or col not in df.columns:
        raise HTTPException(
            400,
            f"URL column not found. Columns: {list(df.columns)}. "
            "Pass url_column in the form.",
        )
    brand_col = brand_column.strip() if brand_column else None
    if not brand_col:
        brand_col = _default_brand_column(list(df.columns))
    if not brand_col or brand_col not in df.columns:
        raise HTTPException(400, brand_column_error(list(df.columns)))
    adv_col = detect_advertiser_name_column(list(df.columns))
    return col, brand_col, adv_col


def _advertiser_for_row(df: pd.DataFrame, idx: int, adv_col: str | None) -> str:
    if not adv_col:
        return ""
    return _cell_str(df.iloc[idx][adv_col])


def _make_row(
    df: pd.DataFrame,
    idx: int,
    url: str,
    brand_col: str,
    adv_col: str | None,
    meta_lookup: dict,
    thumb: Path | None,
    thumb_fetch_detail: str | None = None,
    faulty_col: str | None = None,
    thumb_remote: str | None = None,
) -> dict:
    pre_fault = (
        parse_is_faulty_value(df.iloc[idx][faulty_col])
        if faulty_col
        else False
    )
    row = {
        "index": int(idx),
        "url": url,
        "brandName": _cell_str(df.iloc[idx][brand_col]),
        "advertiserName": _advertiser_for_row(df, idx, adv_col),
        "thumb": str(thumb) if thumb else None,
        "thumbRemote": thumb_remote,
        "metadata": row_metadata(df, idx, meta_lookup),
        "isFault": pre_fault,
        "advertiserMatch": None,
        "reviewed": False,
        "needsIndividualReview": False,
        "faultManual": pre_fault,
    }
    if thumb_fetch_detail:
        row["thumbFetchDetail"] = thumb_fetch_detail
    return row


def _resolve_row_thumb(
    url: str,
    thumb: Path | None,
    remote: str | None,
    fetch_failures: dict[str, str],
) -> tuple[Path | None, str | None, str | None]:
    """Validate local thumb; prefer remote URL when download was skipped."""
    if remote:
        return None, remote, None
    validated = _validated_thumb(thumb, url, fetch_failures)
    if validated:
        return validated, None, None
    if url and is_youtube_url(url):
        yt = _youtube_ui_thumb(url)
        if yt:
            return None, yt, None
    detail = fetch_failures.get(url) if url and not validated else None
    return None, None, detail


def _urls_from_column(df: pd.DataFrame, col: str) -> list[str]:
    return [_cell_str(raw) for raw in df[col].astype(str)]


async def _download_rows(
    df: pd.DataFrame,
    col: str,
    brand_col: str,
    adv_col: str | None,
    cache_dir: Path,
    meta_lookup: dict,
    on_progress=None,
) -> list[dict]:
    urls = _urls_from_column(df, col)
    total = len(urls)
    unique_total = len({u for u in urls if u})
    row_count = 0
    downloaded = 0

    def batch_progress(done: int, _unique: int, dl: int) -> None:
        nonlocal row_count, downloaded
        row_count = done
        downloaded = dl

    thumb_by_url, fetch_failures, remote_by_url = await asyncio.to_thread(
        resolve_thumbnails_batch,
        urls,
        cache_dir,
        thumbnail_concurrency(),
        batch_progress,
    )

    faulty_col = detect_is_faulty_column(list(df.columns))
    rows: list[dict] = []
    row_downloaded = 0
    for idx, url in enumerate(urls):
        thumb, remote, detail = _resolve_row_thumb(
            url,
            thumb_by_url.get(url) if url else None,
            remote_by_url.get(url) if url else None,
            fetch_failures,
        )
        if thumb or remote:
            row_downloaded += 1
        rows.append(
            _make_row(
                df,
                idx,
                url,
                brand_col,
                adv_col,
                meta_lookup,
                thumb,
                detail,
                faulty_col,
                remote,
            )
        )
        if on_progress:
            approx = int((idx + 1) / max(total, 1) * unique_total)
            await on_progress(approx, total, row_downloaded)

    return rows


def _finalize_session(
    session_id: str,
    df: pd.DataFrame,
    col: str,
    brand_col: str,
    adv_col: str | None,
    rows: list[dict],
    source_filename: str = "",
) -> dict:
    with_url = [r for r in rows if r.get("url")]
    if not with_url:
        raise HTTPException(400, "No URLs found in file.")

    def to_items(members: list[dict]) -> list[dict]:
        return _member_items(session_id, members)

    group_list, uncertain_list, unavailable_media = build_brand_groups(
        with_url, to_items
    )
    if not group_list and not uncertain_list and not unavailable_media:
        raise HTTPException(400, "No groupable rows found.")

    _finalize_session_thumbs(session_id, rows)

    _session_put(
        session_id,
        {
            "df": df,
            "url_column": col,
            "brand_column": brand_col,
            "adv_column": adv_col,
            "sourceFilename": source_filename or _load_source_filename(session_id),
            "rows": rows,
            "groups": group_list,
            "uncertain": uncertain_list,
            "unavailable": unavailable_media,
            "group_cursor": 0,
            "uncertain_cursor": 0,
            "unavailable_reviewed": False,
        },
    )

    return {
        "sessionId": session_id,
        "totalRows": len(rows),
        "downloaded": sum(1 for r in rows if _row_has_thumb(r)),
        "groupCount": len(group_list),
        "uncertainCount": len(uncertain_list),
        "unavailableCount": unavailable_media["count"] if unavailable_media else 0,
        "groups": group_list,
        "uncertain": uncertain_list,
        "unavailable": unavailable_media,
        "groupingMethod": "brand",
        "warnings": _opencv_warnings(),
    }


@app.get("/", response_class=HTMLResponse)
async def index():
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


@app.get("/api/hints")
async def get_hints():
    return {"hints": load_hints(), "hintRotateMs": hint_rotate_ms()}


@app.get("/api/diagnostics")
async def get_diagnostics():
    """Environment check (OpenCV / Python path) — useful when video thumbs all fail."""
    info = opencv_diagnostics()
    info["serverless"] = _is_serverless()
    info["dataDir"] = str(DATA_DIR)
    info["dataDirWritable"] = DATA_DIR.exists() and os.access(DATA_DIR, os.W_OK)
    info["blobEnabled"] = _blob_enabled()
    info["blobSessionEnabled"] = _blob_session_enabled()
    info["blobThumbsEnabled"] = _blob_thumbs_enabled()
    info["blobTokenPresent"] = bool(os.environ.get("BLOB_READ_WRITE_TOKEN"))
    info["clientSideReview"] = True
    info["blobLibAvailable"] = True
    info["maxUploadBytes"] = MAX_UPLOAD_BYTES
    info["vercelPayloadLimit"] = _is_serverless()
    return info


def _job_update(job_id: str, **fields) -> None:
    job = _job_get(job_id)
    if job:
        job.update(fields)
        _job_set(job_id, job)


async def _process_job(
    job_id: str,
    df: pd.DataFrame,
    col: str,
    brand_col: str,
    adv_col: str | None,
    meta_lookup: dict,
    cache_dir: Path,
) -> None:
    job = _job_get(job_id)
    if not job:
        return
    hints = load_hints()
    hint_i = 0

    def next_hint() -> str:
        nonlocal hint_i
        h = hints[hint_i % len(hints)]
        hint_i += 1
        return h

    try:
        total = len(df)
        _job_update(
            job_id,
            phase="download",
            percent=5,
            hint="Downloading creatives…",
            current=0,
            total=total,
            downloaded=0,
        )

        urls = _urls_from_column(df, col)
        unique_total = len({u for u in urls if u})
        workers = thumbnail_concurrency()

        _job_update(
            job_id,
            phase="download",
            percent=5,
            hint=(
                f"Downloading {unique_total} unique URLs "
                f"({total} rows, {workers} parallel workers)…"
            ),
            current=0,
            total=total,
            downloaded=0,
        )

        def batch_progress(done: int, _unique: int, dl: int) -> None:
            pct = 5 + int((done / max(unique_total, 1)) * 85)
            _job_update(
                job_id,
                current=done,
                total=total,
                percent=min(pct, 90),
                hint=f"Fetched {done} of {unique_total} unique URLs… {next_hint()}",
                downloaded=dl,
            )

        thumb_by_url, fetch_failures, remote_by_url = await asyncio.to_thread(
            resolve_thumbnails_batch,
            urls,
            cache_dir,
            workers,
            batch_progress,
        )

        faulty_col = detect_is_faulty_column(list(df.columns))
        rows = []
        for idx, url in enumerate(urls):
            thumb, remote, detail = _resolve_row_thumb(
                url,
                thumb_by_url.get(url) if url else None,
                remote_by_url.get(url) if url else None,
                fetch_failures,
            )
            rows.append(
                _make_row(
                    df,
                    idx,
                    url,
                    brand_col,
                    adv_col,
                    meta_lookup,
                    thumb,
                    detail,
                    faulty_col,
                    remote,
                )
            )
            if idx % 5 == 0 or idx + 1 == total:
                pct = 90 + int(((idx + 1) / max(total, 1)) * 4)
                _job_update(
                    job_id,
                    current=idx + 1,
                    total=total,
                    percent=min(pct, 94),
                    phase="download",
                    downloaded=sum(1 for r in rows if _row_has_thumb(r)),
                )

        _job_update(
            job_id,
            phase="group",
            percent=95,
            hint="Grouping creatives by brand…",
            current=total,
        )

        result = _finalize_session(
            job_id,
            df,
            col,
            brand_col,
            adv_col,
            rows,
            _load_source_filename(job_id),
        )
        _job_update(
            job_id,
            status="complete",
            percent=100,
            phase="done",
            hint="Ready to review!",
            result=result,
        )
    except HTTPException as exc:
        detail = exc.detail
        if isinstance(detail, list):
            detail = "; ".join(str(d) for d in detail)
        _job_update(job_id, status="error", error=str(detail), percent=0)
    except Exception as exc:
        logger.exception("Job %s failed", job_id)
        _job_update(job_id, status="error", error=str(exc), percent=0)


@app.post("/api/preview-columns")
async def preview_columns(file: UploadFile = File(...)):
    """Read spreadsheet headers so the client can offer column pickers before upload."""
    filename = file.filename or ""
    raw = await file.read()
    _reject_oversized_upload(raw)
    df = await asyncio.to_thread(_load_dataframe, raw, filename)
    columns = list(df.columns)
    return {
        "filename": filename,
        "columns": columns,
        "defaultUrlColumn": _default_url_column(columns),
        "defaultBrandColumn": _default_brand_column(columns),
    }


@app.post("/api/jobs")
async def create_job(
    file: UploadFile = File(...),
    url_column: str = Form(""),
    brand_column: str = Form(""),
):
    """Start processing; poll GET /api/jobs/{id} unless serverless returns result inline."""
    try:
        _ensure_data_dirs()
        raw = await file.read()
        _reject_oversized_upload(raw)
        df = await asyncio.to_thread(_load_dataframe, raw, file.filename or "")
        col, brand_col, adv_col = _resolve_columns(df, url_column, brand_column)
        meta_lookup = column_lookup(df)

        job_id = str(uuid.uuid4())
        _persist_upload_source(job_id, raw, file.filename or "")
        cache_dir = _session_dir(job_id) / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)

        _job_set(
            job_id,
            {
                "status": "running",
                "phase": "starting",
                "percent": 2,
                "hint": "File received — starting…",
                "current": 0,
                "total": len(df),
                "downloaded": 0,
                "error": None,
                "result": None,
            },
        )

        base_payload = {
            "jobId": job_id,
            "total": len(df),
            "hints": load_hints(),
            "hintRotateMs": hint_rotate_ms(),
            "warnings": _opencv_warnings(),
        }

        if _is_serverless():
            await _process_job(
                job_id, df, col, brand_col, adv_col, meta_lookup, cache_dir
            )
            job = _job_get(job_id) or {}
            return {
                **base_payload,
                "status": job.get("status", "error"),
                "result": job.get("result"),
                "error": job.get("error"),
            }

        asyncio.create_task(
            _process_job(
                job_id, df, col, brand_col, adv_col, meta_lookup, cache_dir
            )
        )
        return base_payload
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("create_job failed")
        detail = f"{type(exc).__name__}: {exc}"
        raise HTTPException(500, detail) from exc


@app.get("/api/jobs/{job_id}")
async def get_job_status(job_id: str):
    job = _job_get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")

    payload = {
        "status": job["status"],
        "phase": job.get("phase", ""),
        "percent": job.get("percent", 0),
        "hint": job.get("hint", ""),
        "current": job.get("current", 0),
        "total": job.get("total", 0),
        "downloaded": job.get("downloaded", 0),
    }
    if job["status"] == "complete" and job.get("result"):
        payload["result"] = job["result"]
    if job["status"] == "error":
        payload["error"] = job.get("error", "Unknown error")
    return payload


@app.post("/api/upload-stream")
async def upload_stream(
    file: UploadFile = File(...),
    url_column: str = Form(""),
    brand_column: str = Form(""),
):
    filename = file.filename or ""
    hints = load_hints()
    hint_idx = 0

    def next_hint() -> str:
        nonlocal hint_idx
        h = hints[hint_idx % len(hints)]
        hint_idx += 1
        return h

    async def events() -> AsyncIterator[str]:
        try:
            _ensure_data_dirs()
            yield _stream_line(
                {
                    "type": "progress",
                    "phase": "receive",
                    "current": 0,
                    "total": 0,
                    "hint": "Connected — receiving your file…",
                    "percent": 1,
                }
            )
            await asyncio.sleep(0)

            raw = await file.read()
            _reject_oversized_upload(raw)
            yield _stream_line(
                {
                    "type": "progress",
                    "phase": "parse",
                    "current": 0,
                    "total": 0,
                    "hint": next_hint(),
                    "percent": 3,
                }
            )
            await asyncio.sleep(0)

            df = await asyncio.to_thread(_load_dataframe, raw, filename)
            col, brand_col, adv_col = _resolve_columns(df, url_column, brand_column)
            meta_lookup = column_lookup(df)
            faulty_col = detect_is_faulty_column(list(df.columns))
            total = len(df)

            session_id = str(uuid.uuid4())
            _persist_upload_source(session_id, raw, filename)
            cache_dir = _session_dir(session_id) / "cache"
            cache_dir.mkdir(parents=True, exist_ok=True)

            urls = _urls_from_column(df, col)
            unique_total = len({u for u in urls if u})
            workers = thumbnail_concurrency()
            progress = {"done": 0, "downloaded": 0}

            def batch_progress(done: int, _unique: int, dl: int) -> None:
                progress["done"] = done
                progress["downloaded"] = dl

            yield _stream_line(
                {
                    "type": "progress",
                    "phase": "download",
                    "current": 0,
                    "total": total,
                    "downloaded": 0,
                    "hint": (
                        f"Downloading {unique_total} unique URLs "
                        f"({total} rows, {workers} workers)…"
                    ),
                    "percent": 5,
                    "status": "fetching",
                }
            )
            await asyncio.sleep(0)

            batch_task = asyncio.create_task(
                asyncio.to_thread(
                    resolve_thumbnails_batch,
                    urls,
                    cache_dir,
                    workers,
                    batch_progress,
                )
            )
            while not batch_task.done():
                done = progress["done"]
                dl = progress["downloaded"]
                pct = 5 + int((done / max(unique_total, 1)) * 85)
                yield _stream_line(
                    {
                        "type": "progress",
                        "phase": "download",
                        "current": done,
                        "total": total,
                        "downloaded": dl,
                        "hint": (
                            f"Fetched {done} of {unique_total} unique URLs… "
                            f"{next_hint()}"
                        ),
                        "percent": min(pct, 90),
                        "status": "working",
                    }
                )
                await asyncio.sleep(0.25)

            thumb_by_url, fetch_failures, remote_by_url = batch_task.result()
            rows = []
            downloaded = 0
            for idx, url in enumerate(urls):
                thumb, remote, detail = _resolve_row_thumb(
                    url,
                    thumb_by_url.get(url) if url else None,
                    remote_by_url.get(url) if url else None,
                    fetch_failures,
                )
                if thumb or remote:
                    downloaded += 1
                rows.append(
                    _make_row(
                        df,
                        idx,
                        url,
                        brand_col,
                        adv_col,
                        meta_lookup,
                        thumb,
                        detail,
                        faulty_col,
                        remote,
                    )
                )
                if idx % 5 == 0 or idx + 1 == total:
                    pct = 90 + int(((idx + 1) / max(total, 1)) * 4)
                    yield _stream_line(
                        {
                            "type": "progress",
                            "phase": "download",
                            "current": idx + 1,
                            "total": total,
                            "downloaded": downloaded,
                            "hint": f"Building rows {idx + 1} / {total}…",
                            "percent": min(pct, 94),
                            "status": "working",
                        }
                    )
                    await asyncio.sleep(0)

            yield _stream_line(
                {
                    "type": "progress",
                    "phase": "download",
                    "current": total,
                    "total": total,
                    "downloaded": downloaded,
                    "hint": next_hint(),
                    "percent": 90,
                    "status": "done",
                }
            )
            await asyncio.sleep(0)

            yield _stream_line(
                {
                    "type": "progress",
                    "phase": "group",
                    "current": total,
                    "total": total,
                    "hint": "Grouping creatives by brand…",
                    "percent": 95,
                }
            )
            await asyncio.sleep(0)

            payload = _finalize_session(
                session_id, df, col, brand_col, adv_col, rows, filename
            )
            payload["type"] = "complete"
            payload["percent"] = 100
            yield _stream_line(payload)
        except HTTPException as exc:
            yield _stream_line({"type": "error", "detail": exc.detail})
        except Exception as exc:
            logger.exception("Upload stream failed")
            yield _stream_line({"type": "error", "detail": str(exc)})

    return StreamingResponse(
        events(),
        media_type="application/x-ndjson",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _stream_line(payload: dict) -> str:
    return json.dumps(payload, default=str) + "\n"


@app.post("/api/upload")
async def upload(
    file: UploadFile = File(...),
    url_column: str = Form(""),
    brand_column: str = Form(""),
    k_groups: int = Form(0),  # kept for API compat; ignored (brand grouping)
):
    try:
        return await _process_upload(file, url_column, brand_column)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Upload failed")
        return JSONResponse(status_code=500, content={"detail": str(e)})


async def _process_upload(file: UploadFile, url_column: str, brand_column: str = ""):
    _ensure_data_dirs()
    raw = await file.read()
    df = await asyncio.to_thread(_load_dataframe, raw, file.filename or "")
    col, brand_col, adv_col = _resolve_columns(df, url_column, brand_column)
    meta_lookup = column_lookup(df)
    session_id = str(uuid.uuid4())
    _persist_upload_source(session_id, raw, file.filename or "")
    cache_dir = _session_dir(session_id) / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    rows = await _download_rows(df, col, brand_col, adv_col, cache_dir, meta_lookup)
    return _finalize_session(
        session_id,
        df,
        col,
        brand_col,
        adv_col,
        rows,
        file.filename or "",
    )


@app.get("/api/thumb/{session_id}/{filename}")
async def serve_thumb(session_id: str, filename: str):
    cache = _session_dir(session_id) / "cache"
    path = cache / filename
    if path.exists() and str(path.resolve()).startswith(str(cache.resolve())):
        return FileResponse(path)
    if _blob_enabled():
        blob = _blob_find(_thumb_blob_pathname(session_id, filename))
        if blob:
            url = blob.get("downloadUrl") or blob.get("url")
            if url:
                return RedirectResponse(url, status_code=307)
    raise HTTPException(404)


@app.post("/api/session/{session_id}/review")
async def review_group(session_id: str, body: dict):
    sess = _session_get(session_id)
    if not sess:
        raise HTTPException(404, "Session not found")

    group_id = body.get("groupId")
    is_fault = bool(body.get("isFault", False))
    if group_id is None:
        raise HTTPException(400, "groupId required")

    group = next((g for g in sess["groups"] if g["groupId"] == group_id), None)
    if not group:
        raise HTTPException(404, "Group not found")

    for idx in group["memberIndices"]:
        for row in sess["rows"]:
            if row["index"] == idx:
                if is_fault:
                    row["isFault"] = True
                elif not row.get("faultManual"):
                    row["isFault"] = False
                row["advertiserMatch"] = True
                row["reviewed"] = True

    sess["group_cursor"] = min(sess["group_cursor"] + 1, len(sess["groups"]))
    _session_put(session_id, sess, full=True)

    return {"ok": True, "cursor": sess["group_cursor"]}


@app.post("/api/session/{session_id}/toggle-fault")
async def toggle_fault(session_id: str, body: dict):
    sess = _session_get(session_id)
    if not sess:
        logger.info("toggle_fault: session_missing session_id=%s", session_id)
        raise HTTPException(404, "Session not found")

    row_index = body.get("rowIndex")
    if row_index is None:
        logger.info("toggle_fault: rowIndex_missing session_id=%s", session_id)
        raise HTTPException(400, "rowIndex required")
    try:
        row_index = int(row_index)
    except Exception:
        logger.info("toggle_fault: rowIndex_invalid session_id=%s rowIndex=%r", session_id, row_index)
        raise HTTPException(400, "rowIndex must be an integer") from None

    is_fault = body.get("isFault")
    if is_fault is None:
        for row in sess["rows"]:
            if row["index"] == row_index:
                is_fault = not row.get("isFault", False)
                break
        else:
            logger.info(
                "toggle_fault: row_missing_infer session_id=%s rowIndex=%s rows=%s",
                session_id,
                row_index,
                len(sess.get("rows", [])),
            )
            raise HTTPException(422, "Row not found")
    else:
        is_fault = bool(is_fault)

    for row in sess["rows"]:
        if row["index"] == row_index:
            row["isFault"] = is_fault
            row["faultManual"] = is_fault
            if not is_fault:
                row["faultManual"] = False
            break
    else:
        logger.info(
            "toggle_fault: row_missing_apply session_id=%s rowIndex=%s rows=%s",
            session_id,
            row_index,
            len(sess.get("rows", [])),
        )
        raise HTTPException(422, "Row not found")

    _session_put(session_id, sess, full=False)
    return {"ok": True, "rowIndex": row_index, "isFault": is_fault}


@app.post("/api/session/{session_id}/review-unavailable")
async def review_unavailable(session_id: str, body: dict):
    """Mark all unavailable-media rows reviewed after the single table swipe."""
    sess = _session_get(session_id)
    if not sess:
        raise HTTPException(404, "Session not found")

    unavailable = sess.get("unavailable")
    if not unavailable:
        raise HTTPException(404, "No unavailable media session")

    is_fault = bool(body.get("isFault", False))
    indices = {int(e["rowIndex"]) for e in unavailable.get("entries", [])}

    for row in sess["rows"]:
        if row["index"] in indices:
            row["reviewed"] = True
            row["advertiserMatch"] = not is_fault
            if is_fault:
                row["isFault"] = True

    sess["unavailable_reviewed"] = True
    _session_put(session_id, sess, full=True)
    return {"ok": True, "count": len(indices)}


@app.post("/api/session/{session_id}/review-uncertain")
async def review_uncertain_group(session_id: str, body: dict):
    """Confirm or reject brand labels for all creatives in an uncertain brand group."""
    sess = _session_get(session_id)
    if not sess:
        raise HTTPException(404, "Session not found")

    group_id = body.get("groupId")
    advertiser_match = bool(body.get("advertiserMatch", False))
    if group_id is None:
        raise HTTPException(400, "groupId required")

    group = next(
        (g for g in sess.get("uncertain", []) if g["groupId"] == group_id),
        None,
    )
    if not group:
        raise HTTPException(404, "Uncertain group not found")

    indices = {int(i) for i in group.get("memberIndices", [])}
    for row in sess["rows"]:
        if row["index"] in indices:
            row["advertiserMatch"] = advertiser_match
            row["reviewed"] = True
            if not advertiser_match:
                row["isFault"] = True
            elif not row.get("faultManual"):
                row["isFault"] = False

    sess["uncertain_cursor"] = min(
        sess["uncertain_cursor"] + 1, len(sess.get("uncertain", []))
    )
    _session_put(session_id, sess, full=True)
    return {"ok": True, "cursor": sess["uncertain_cursor"]}


@app.post("/api/session/{session_id}/review-item")
async def review_item(session_id: str, body: dict):
    """Confirm whether the creative matches its advertiser label (single row)."""
    sess = _session_get(session_id)
    if not sess:
        raise HTTPException(404, "Session not found")

    row_index = body.get("rowIndex")
    advertiser_match = bool(body.get("advertiserMatch", False))
    if row_index is None:
        raise HTTPException(400, "rowIndex required")

    for row in sess["rows"]:
        if row["index"] == row_index:
            row["advertiserMatch"] = advertiser_match
            row["reviewed"] = True
            if not advertiser_match:
                row["isFault"] = True
            break
    else:
        raise HTTPException(404, "Row not found")

    sess["uncertain_cursor"] = min(
        sess["uncertain_cursor"] + 1, len(sess.get("uncertain", []))
    )
    _session_put(session_id, sess, full=True)

    return {"ok": True, "cursor": sess["uncertain_cursor"]}


@app.post("/api/session/{session_id}/export")
async def export_xlsx_with_review(session_id: str, body: dict):
    """Export annotated spreadsheet using browser-stored review state (Tier B)."""
    review_rows = body.get("rows") or {}
    if not review_rows:
        raise HTTPException(400, "Review rows required — complete review in browser first.")

    download_name = _export_download_name(session_id, _session_get(session_id))
    sess = _session_get(session_id)
    if sess:
        try:
            _merge_review_snapshot(sess, {"rows": review_rows})
            data = await asyncio.to_thread(_build_export_bytes, sess)
            return _export_file_response(data, download_name)
        except Exception as exc:
            logger.exception("Export from session failed %s: %s", session_id, exc)

    data = await asyncio.to_thread(_build_export_from_client_review, session_id, review_rows)
    if data:
        return _export_file_response(data, download_name)

    raise HTTPException(
        404,
        "Source file not found on server. Re-upload and export from the same browser session.",
    )


@app.get("/api/session/{session_id}/export")
async def export_xlsx(session_id: str):
    sess = _session_get(session_id)
    download_name = _export_download_name(session_id, sess)
    checkpoint = await asyncio.to_thread(_load_export_checkpoint, session_id)

    if sess:
        try:
            data = await asyncio.to_thread(_build_export_bytes, sess)
            await asyncio.to_thread(
                _checkpoint_export, session_id, sess, to_blob=_blob_session_enabled()
            )
            return _export_file_response(data, download_name)
        except Exception as exc:
            logger.exception("Export from session failed %s: %s", session_id, exc)

    if checkpoint:
        return _export_file_response(checkpoint, download_name)

    data = await asyncio.to_thread(_build_export_from_artifacts, session_id)
    if data:
        return _export_file_response(data, download_name)

    if not sess:
        raise HTTPException(
            404,
            "Session not found. Export with POST and your browser review data instead.",
        )
    raise HTTPException(500, "Export failed — please try again.")
