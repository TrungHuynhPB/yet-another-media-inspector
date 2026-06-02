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
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from brand_grouping import build_brand_groups
from hints_loader import hint_rotate_ms, load_hints
from metadata import column_lookup, row_metadata
from metadata import (
    brand_column_error,
    detect_advertiser_name_column,
    detect_brand_column,
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


def _blob_enabled() -> bool:
    # Uses the Blob REST API directly (no SDK dependency).
    return bool(_is_serverless() and os.environ.get("BLOB_READ_WRITE_TOKEN"))


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


def _blob_put_bytes(pathname: str, data: bytes, *, content_type: str) -> None:
    if not _blob_enabled():
        return
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
    except Exception as exc:
        logger.warning("Blob put failed %s: %s", pathname, exc)


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


def _faults_path(session_id: str) -> Path:
    return SESSIONS_DIR / f"{session_id}.faults.json"


def _faults_snapshot(sess: dict) -> dict:
    return {
        "faults": {
            str(r["index"]): {
                "isFault": bool(r.get("isFault")),
                "faultManual": bool(r.get("faultManual")),
            }
            for r in sess.get("rows", [])
        }
    }


def _merge_faults_snapshot(sess: dict, snapshot: dict) -> None:
    faults = snapshot.get("faults") or {}
    for row in sess.get("rows", []):
        state = faults.get(str(row["index"]))
        if state is None:
            continue
        row["isFault"] = bool(state.get("isFault"))
        row["faultManual"] = bool(state.get("faultManual"))


def _write_faults_snapshot(session_id: str, sess: dict) -> None:
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(_faults_snapshot(sess)).encode("utf-8")
    _faults_path(session_id).write_bytes(payload)
    _blob_put_bytes(f"sessions/{session_id}.faults.json", payload, content_type="application/json")


def _session_get(session_id: str) -> dict | None:
    if session_id in _sessions:
        sess = _sessions[session_id]
    else:
        path = SESSIONS_DIR / f"{session_id}.pkl"
        if not path.is_file() and _blob_enabled():
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
        fp = _faults_path(session_id)
        if not fp.is_file() and _blob_enabled():
            data = _blob_get_bytes(f"sessions/{session_id}.faults.json", timeout=15.0)
            if data:
                _ensure_data_dirs()
                fp.write_bytes(data)
        if fp.is_file():
            try:
                _merge_faults_snapshot(sess, json.loads(fp.read_text(encoding="utf-8")))
            except Exception as exc:
                logger.warning("Failed to merge faults for %s: %s", session_id, exc)
    return sess


def _session_put(session_id: str, sess: dict, *, full: bool = True) -> None:
    _sessions[session_id] = sess
    if not _is_serverless():
        return
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    if full:
        path = SESSIONS_DIR / f"{session_id}.pkl"
        data = pickle.dumps(sess, protocol=pickle.HIGHEST_PROTOCOL)
        path.write_bytes(data)
        _blob_put_bytes(f"sessions/{session_id}.pkl", data, content_type="application/octet-stream")
    _write_faults_snapshot(session_id, sess)


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
        if m.get("thumb"):
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


def _resolve_columns(df: pd.DataFrame, url_column: str) -> tuple[str, str, str | None]:
    col = url_column.strip() if url_column else None
    if not col:
        col = detect_url_column(list(df.columns))
    if not col or col not in df.columns:
        raise HTTPException(
            400,
            f"URL column not found. Columns: {list(df.columns)}. "
            "Pass url_column in the form.",
        )
    brand_col = detect_brand_column(list(df.columns))
    if not brand_col:
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
) -> dict:
    row = {
        "index": int(idx),
        "url": url,
        "brandName": _cell_str(df.iloc[idx][brand_col]),
        "advertiserName": _advertiser_for_row(df, idx, adv_col),
        "thumb": str(thumb) if thumb else None,
        "metadata": row_metadata(df, idx, meta_lookup),
        "isFault": False,
        "advertiserMatch": None,
        "reviewed": False,
        "needsIndividualReview": False,
        "faultManual": False,
    }
    if thumb_fetch_detail:
        row["thumbFetchDetail"] = thumb_fetch_detail
    return row


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

    thumb_by_url, fetch_failures = await asyncio.to_thread(
        resolve_thumbnails_batch,
        urls,
        cache_dir,
        thumbnail_concurrency(),
        batch_progress,
    )

    rows: list[dict] = []
    row_downloaded = 0
    for idx, url in enumerate(urls):
        thumb = _validated_thumb(
            thumb_by_url.get(url) if url else None, url, fetch_failures
        )
        if thumb:
            row_downloaded += 1
        detail = fetch_failures.get(url) if url and not thumb else None
        rows.append(
            _make_row(
                df, idx, url, brand_col, adv_col, meta_lookup, thumb, detail
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

    _session_put(
        session_id,
        {
            "df": df,
            "url_column": col,
            "brand_column": brand_col,
            "adv_column": adv_col,
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
        "downloaded": sum(1 for r in rows if r.get("thumb")),
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
    info["blobTokenPresent"] = bool(os.environ.get("BLOB_READ_WRITE_TOKEN"))
    info["blobLibAvailable"] = True
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
                percent=min(pct, 90),
                hint=f"Fetched {done} of {unique_total} unique URLs… {next_hint()}",
                downloaded=dl,
            )

        thumb_by_url, fetch_failures = await asyncio.to_thread(
            resolve_thumbnails_batch,
            urls,
            cache_dir,
            workers,
            batch_progress,
        )

        rows = []
        for idx, url in enumerate(urls):
            thumb = _validated_thumb(
                thumb_by_url.get(url) if url else None, url, fetch_failures
            )
            detail = fetch_failures.get(url) if url and not thumb else None
            rows.append(
                _make_row(
                    df, idx, url, brand_col, adv_col, meta_lookup, thumb, detail
                )
            )

        _job_update(
            job_id,
            phase="group",
            percent=95,
            hint="Grouping creatives by brand…",
            current=total,
        )

        result = _finalize_session(job_id, df, col, brand_col, adv_col, rows)
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


@app.post("/api/jobs")
async def create_job(
    file: UploadFile = File(...),
    url_column: str = Form(""),
):
    """Start processing; poll GET /api/jobs/{id} unless serverless returns result inline."""
    try:
        _ensure_data_dirs()
        raw = await file.read()
        df = await asyncio.to_thread(_load_dataframe, raw, file.filename or "")
        col, brand_col, adv_col = _resolve_columns(df, url_column)
        meta_lookup = column_lookup(df)

        job_id = str(uuid.uuid4())
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
            col, brand_col, adv_col = _resolve_columns(df, url_column)
            meta_lookup = column_lookup(df)
            total = len(df)

            yield _stream_line(
                {
                    "type": "progress",
                    "phase": "download",
                    "current": 0,
                    "total": total,
                    "hint": "Downloading creatives (images & Adclarity/TikTok video posters)…",
                    "percent": 5,
                }
            )
            await asyncio.sleep(0)

            session_id = str(uuid.uuid4())
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
                await asyncio.sleep(2.0)

            thumb_by_url, fetch_failures = batch_task.result()
            rows = []
            downloaded = 0
            for idx, url in enumerate(urls):
                thumb = _validated_thumb(
                    thumb_by_url.get(url) if url else None, url, fetch_failures
                )
                if thumb:
                    downloaded += 1
                detail = fetch_failures.get(url) if url and not thumb else None
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
                    )
                )

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

            payload = _finalize_session(session_id, df, col, brand_col, adv_col, rows)
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
    k_groups: int = Form(0),  # kept for API compat; ignored (brand grouping)
):
    try:
        return await _process_upload(file, url_column)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Upload failed")
        return JSONResponse(status_code=500, content={"detail": str(e)})


async def _process_upload(file: UploadFile, url_column: str):
    _ensure_data_dirs()
    raw = await file.read()
    df = await asyncio.to_thread(_load_dataframe, raw, file.filename or "")
    col, brand_col, adv_col = _resolve_columns(df, url_column)
    meta_lookup = column_lookup(df)
    session_id = str(uuid.uuid4())
    cache_dir = _session_dir(session_id) / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    rows = await _download_rows(df, col, brand_col, adv_col, cache_dir, meta_lookup)
    return _finalize_session(session_id, df, col, brand_col, adv_col, rows)


@app.get("/api/thumb/{session_id}/{filename}")
async def serve_thumb(session_id: str, filename: str):
    cache = _session_dir(session_id) / "cache"
    path = cache / filename
    if not path.exists() or not str(path.resolve()).startswith(str(cache.resolve())):
        raise HTTPException(404)
    return FileResponse(path)


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


@app.get("/api/session/{session_id}/export")
async def export_xlsx(session_id: str):
    sess = _session_get(session_id)
    if not sess:
        raise HTTPException(404, "Session not found")

    df = sess["df"].copy()
    rows_by_idx = {int(r["index"]): r for r in sess["rows"]}
    fault_map = {r["index"]: r["isFault"] for r in sess["rows"]}
    match_map = {r["index"]: r.get("advertiserMatch") for r in sess["rows"]}
    reviewed_map = {r["index"]: r["reviewed"] for r in sess["rows"]}

    df["BRAND"] = [
        rows_by_idx.get(i, {}).get("brandName", "") for i in range(len(df))
    ]
    df["ADVERTISER_NAME"] = [
        rows_by_idx.get(i, {}).get("advertiserName", "") for i in range(len(df))
    ]
    df["isFault"] = [fault_map.get(i, False) for i in range(len(df))]
    df["advertiserMatch"] = [match_map.get(i) for i in range(len(df))]
    df["reviewed"] = [reviewed_map.get(i, False) for i in range(len(df))]

    out_dir = _session_dir(session_id)
    out_path = out_dir / "media_inspector_output.xlsx"
    df.to_excel(out_path, index=False)

    return FileResponse(
        out_path,
        filename="media_inspector_output.xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
