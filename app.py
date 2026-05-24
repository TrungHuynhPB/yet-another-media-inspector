"""Media Inspector — group creatives by brand and swipe-review for faults."""

import json
import logging
import uuid
from io import BytesIO
from pathlib import Path

import httpx
import pandas as pd
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from brand_grouping import build_brand_groups
from media import detect_advertiser_column, detect_url_column, resolve_thumbnail

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="Media Inspector")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

_sessions: dict[str, dict] = {}


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
        }
        if m.get("thumb"):
            item["thumbUrl"] = _thumb_url(session_id, m["thumb"])
        items.append(item)
    return items


@app.get("/", response_class=HTMLResponse)
async def index():
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


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
    name = (file.filename or "").lower()
    raw = await file.read()

    if name.endswith(".json"):
        payload = json.loads(raw.decode("utf-8"))
        if isinstance(payload, list):
            df = pd.DataFrame(payload)
        else:
            df = pd.DataFrame(payload.get("rows", payload.get("data", [payload])))
    elif name.endswith((".xlsx", ".xls")):
        df = pd.read_excel(BytesIO(raw))
    elif name.endswith(".csv"):
        df = pd.read_csv(BytesIO(raw))
    else:
        raise HTTPException(400, "Upload .xlsx, .xls, .csv, or .json")

    col = url_column.strip() if url_column else None
    if not col:
        col = detect_url_column(list(df.columns))
    if not col or col not in df.columns:
        raise HTTPException(
            400,
            f"URL column not found. Columns: {list(df.columns)}. "
            "Pass url_column in the form.",
        )

    adv_col = detect_advertiser_column(list(df.columns))
    if not adv_col:
        raise HTTPException(
            400,
            "Advertiser column not found (e.g. advertiser_name, advertise_name). "
            f"Columns: {list(df.columns)}",
        )

    session_id = str(uuid.uuid4())
    cache_dir = _session_dir(session_id) / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    rows = []
    async with httpx.AsyncClient(
        limits=httpx.Limits(max_connections=8),
        follow_redirects=True,
    ) as client:
        for idx, url in enumerate(df[col].astype(str)):
            url = _cell_str(url)
            advertiser = _cell_str(df.iloc[idx][adv_col])
            if not url:
                thumb = None
            else:
                thumb = await resolve_thumbnail(client, url, cache_dir)
            rows.append(
                {
                    "index": int(idx),
                    "url": url,
                    "advertiserName": advertiser,
                    "thumb": str(thumb) if thumb else None,
                    "isFault": False,
                    "advertiserMatch": None,
                    "reviewed": False,
                    "needsIndividualReview": False,
                    "faultManual": False,
                }
            )

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
        "adv_column": adv_col,
        "rows": rows,
        "groups": group_list,
        "uncertain": uncertain_list,
        "group_cursor": 0,
        "uncertain_cursor": 0,
    }

    downloaded = sum(1 for r in rows if r.get("thumb"))

    return {
        "sessionId": session_id,
        "totalRows": len(rows),
        "downloaded": downloaded,
        "groupCount": len(group_list),
        "uncertainCount": len(uncertain_list),
        "groups": group_list,
        "uncertain": uncertain_list,
        "groupingMethod": "brand",
    }


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
    fault_map = {r["index"]: r["isFault"] for r in sess["rows"]}
    match_map = {r["index"]: r.get("advertiserMatch") for r in sess["rows"]}
    reviewed_map = {r["index"]: r["reviewed"] for r in sess["rows"]}

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
