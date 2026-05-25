"""Media Inspector — group creatives by brand and swipe-review for faults."""

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from io import BytesIO
from pathlib import Path

import httpx
import pandas as pd
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from brand_grouping import build_brand_groups
from hints_loader import load_hints
from metadata import column_lookup, row_metadata
from metadata import (
    brand_column_error,
    detect_advertiser_name_column,
    detect_brand_column,
    prepare_upload_dataframe,
)
from media import detect_url_column, resolve_thumbnail_blocking

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="Media Inspector")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

_sessions: dict[str, dict] = {}
_jobs: dict[str, dict] = {}


def _session_dir(session_id: str) -> Path:
    return DATA_DIR / session_id


def _cell_str(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    s = str(value).strip()
    return "" if s.lower() in ("nan", "none") else s


def _thumb_url(session_id: str, thumb_path: str) -> str:
    return f"/api/thumb/{session_id}/{Path(thumb_path).name}"


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
        items.append(item)
    return items


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
        return prepare_upload_dataframe(pd.read_csv(BytesIO(raw)))
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
) -> dict:
    return {
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


async def _download_rows(
    df: pd.DataFrame,
    col: str,
    brand_col: str,
    adv_col: str | None,
    cache_dir: Path,
    meta_lookup: dict,
    on_progress=None,
) -> list[dict]:
    url_series = df[col].astype(str)
    total = len(url_series)
    rows: list[dict] = []
    downloaded = 0

    for idx, raw_url in enumerate(url_series):
        url = _cell_str(raw_url)
        thumb = None
        if url:
            thumb = await asyncio.to_thread(
                resolve_thumbnail_blocking, url, cache_dir
            )
            if thumb:
                downloaded += 1
        rows.append(_make_row(df, idx, url, brand_col, adv_col, meta_lookup, thumb))
        if on_progress:
            await on_progress(idx + 1, total, downloaded)

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

    group_list, uncertain_list = build_brand_groups(with_url, to_items)
    if not group_list and not uncertain_list:
        raise HTTPException(400, "No groupable rows found.")

    _sessions[session_id] = {
        "df": df,
        "url_column": col,
        "brand_column": brand_col,
        "adv_column": adv_col,
        "rows": rows,
        "groups": group_list,
        "uncertain": uncertain_list,
        "group_cursor": 0,
        "uncertain_cursor": 0,
    }

    return {
        "sessionId": session_id,
        "totalRows": len(rows),
        "downloaded": sum(1 for r in rows if r.get("thumb")),
        "groupCount": len(group_list),
        "uncertainCount": len(uncertain_list),
        "groups": group_list,
        "uncertain": uncertain_list,
        "groupingMethod": "brand",
    }


@app.get("/", response_class=HTMLResponse)
async def index():
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


@app.get("/api/hints")
async def get_hints():
    return {"hints": load_hints()}


def _job_update(job_id: str, **fields) -> None:
    job = _jobs.get(job_id)
    if job:
        job.update(fields)


async def _process_job(
    job_id: str,
    df: pd.DataFrame,
    col: str,
    brand_col: str,
    adv_col: str | None,
    meta_lookup: dict,
    cache_dir: Path,
) -> None:
    job = _jobs[job_id]
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

        rows = []
        url_series = df[col].astype(str)
        downloaded = 0

        for idx, raw_url in enumerate(url_series):
            url = _cell_str(raw_url)
            pct = 5 + int((idx / max(total, 1)) * 85)

            _job_update(
                job_id,
                current=idx,
                percent=min(pct, 90),
                hint=f"Fetching row {idx + 1} of {total}…",
                downloaded=downloaded,
            )

            thumb = None
            if url:
                thumb = await asyncio.to_thread(
                    resolve_thumbnail_blocking, url, cache_dir
                )
                if thumb:
                    downloaded += 1

            rows.append(_make_row(df, idx, url, brand_col, adv_col, meta_lookup, thumb))

            _job_update(
                job_id,
                current=idx + 1,
                percent=min(5 + int(((idx + 1) / max(total, 1)) * 85), 90),
                hint=next_hint(),
                downloaded=downloaded,
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
    """Start background processing; poll GET /api/jobs/{id} for progress."""
    raw = await file.read()
    df = await asyncio.to_thread(_load_dataframe, raw, file.filename or "")
    col, brand_col, adv_col = _resolve_columns(df, url_column)
    meta_lookup = column_lookup(df)

    job_id = str(uuid.uuid4())
    cache_dir = _session_dir(job_id) / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    _jobs[job_id] = {
        "status": "running",
        "phase": "starting",
        "percent": 2,
        "hint": "File received — starting…",
        "current": 0,
        "total": len(df),
        "downloaded": 0,
        "error": None,
        "result": None,
    }

    asyncio.create_task(
        _process_job(job_id, df, col, brand_col, adv_col, meta_lookup, cache_dir)
    )

    return {
        "jobId": job_id,
        "total": len(df),
        "hints": load_hints(),
    }


@app.get("/api/jobs/{job_id}")
async def get_job_status(job_id: str):
    job = _jobs.get(job_id)
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

            rows = []
            url_series = df[col].astype(str)
            downloaded = 0
            for idx, raw_url in enumerate(url_series):
                url = _cell_str(raw_url)
                base_pct = 5 + int((idx / max(total, 1)) * 85)

                yield _stream_line(
                    {
                        "type": "progress",
                        "phase": "download",
                        "current": idx,
                        "total": total,
                        "downloaded": downloaded,
                        "hint": f"Fetching row {idx + 1} of {total}…",
                        "percent": min(base_pct, 90),
                        "status": "fetching",
                    }
                )
                await asyncio.sleep(0)

                thumb = None
                if url:
                    task = asyncio.create_task(
                        asyncio.to_thread(resolve_thumbnail_blocking, url, cache_dir)
                    )
                    while not task.done():
                        _, pending = await asyncio.wait({task}, timeout=2.0)
                        if pending:
                            yield _stream_line(
                                {
                                    "type": "progress",
                                    "phase": "download",
                                    "current": idx,
                                    "total": total,
                                    "downloaded": downloaded,
                                    "hint": (
                                        f"Still on row {idx + 1}/{total} "
                                        f"(videos may take ~30s)… {next_hint()}"
                                    ),
                                    "percent": min(base_pct, 90),
                                    "status": "working",
                                }
                            )
                            await asyncio.sleep(0)
                    thumb = task.result()
                    if thumb:
                        downloaded += 1

                rows.append(
                    _make_row(df, idx, url, brand_col, adv_col, meta_lookup, thumb)
                )
                done_pct = 5 + int(((idx + 1) / max(total, 1)) * 85)
                yield _stream_line(
                    {
                        "type": "progress",
                        "phase": "download",
                        "current": idx + 1,
                        "total": total,
                        "downloaded": downloaded,
                        "hint": next_hint(),
                        "percent": min(done_pct, 90),
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
    raw = await file.read()
    df = _load_dataframe(raw, file.filename or "")
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
    sess = _sessions.get(session_id)
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

    return {"ok": True, "cursor": sess["group_cursor"]}


@app.post("/api/session/{session_id}/toggle-fault")
async def toggle_fault(session_id: str, body: dict):
    sess = _sessions.get(session_id)
    if not sess:
        raise HTTPException(404, "Session not found")

    row_index = body.get("rowIndex")
    if row_index is None:
        raise HTTPException(400, "rowIndex required")

    is_fault = body.get("isFault")
    if is_fault is None:
        for row in sess["rows"]:
            if row["index"] == row_index:
                is_fault = not row.get("isFault", False)
                break
        else:
            raise HTTPException(404, "Row not found")
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
        raise HTTPException(404, "Row not found")

    return {"ok": True, "rowIndex": int(row_index), "isFault": is_fault}


@app.post("/api/session/{session_id}/review-item")
async def review_item(session_id: str, body: dict):
    """Confirm whether the creative matches its advertiser label."""
    sess = _sessions.get(session_id)
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

    return {"ok": True, "cursor": sess["uncertain_cursor"]}


@app.get("/api/session/{session_id}/export")
async def export_xlsx(session_id: str):
    sess = _sessions.get(session_id)
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
