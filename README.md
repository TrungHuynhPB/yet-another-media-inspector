# Media Inspector

Group creative ads **by brand** (advertiser name) and swipe-review them. Exports Excel with `isFault` and `advertiserMatch` columns.


Cat with wry smile
YAMI — Yet Another Media Inspector
(https://yet-another-media-inspector.vercel.app)

Feature:
+ attempt to group similar creatives together for ease of creative classification
+ Tinder swap (left arrow = mark as fault, right arrow = OK pass)
+ left click = toggle mark as Fault
+ right click = inspect media (pop-up)
+ Fault group = Mark all creatives in the group as faulty vs OK group (continue to next group)
+ Buy me a coffee

Requirement:
+ input file: must have BRAND & CREATIVE_URL_SUPPLIER columns


Status:
+ works well with ad-clarity urls, for tiktok & youtube still have some issues on fetching media
+ works with 5000 rows or less 

## Setup

```bash
.\.venv\Scripts\activate
uv pip install -r requirements.txt
```

**opencv-python** is required for video posters (AdClarity MP4, etc.). It is listed in `requirements.txt`. If it is missing, every video creative may appear as **Unavailable video** in review.

### Faster downloads (2k–5k+ rows)

Thumbnails are fetched **in parallel** (default **24 workers**). Identical URLs in your sheet are downloaded **once** and reused for every row.

Set worker count before starting the app:

```powershell
$env:YAMI_THUMBNAIL_WORKERS = "32"
python main.py
```

Try `24`–`40` on a good connection; lower to `12`–`16` if you hit rate limits or heavy Adclarity video download load. At ~527 rows, sequential fetch was ~15+ minutes; parallel + dedup typically cuts that to a few minutes when URLs are mostly images/YouTube/TikTok posters.

Thumbnails are resolved automatically when possible:

- **YouTube** — tries several `img.youtube.com` poster sizes
- **TikTok** — TikTok oEmbed (`thumbnail_url`) using `@channel/video/{id}`
- **Adclarity video** — first tries the sibling **JPEG** URL (`…_video.mp4` → `….jpeg`, drop `_video`), then downloads the MP4 (partial or full) and extracts a frame with **OpenCV** if needed

### AdClarity tuning

AdClarity CDN URLs often work in a browser but fail under heavy parallel server fetch. Tune with:

| Variable | Default | Purpose |
|----------|---------|---------|
| `YAMI_THUMBNAIL_WORKERS` | `24` | Global parallel thumbnail workers |
| `YAMI_ADCLARITY_MAX_PARALLEL` | `4` | Max concurrent requests per AdClarity host |
| `YAMI_ADCLARITY_VIDEO_TIMEOUT` | `120` | Seconds for full AdClarity MP4 download (`YAMI_ADCLARITY_FFMPEG_TIMEOUT` still accepted) |

Example (PowerShell):

```powershell
$env:YAMI_ADCLARITY_MAX_PARALLEL = "4"
$env:YAMI_ADCLARITY_VIDEO_TIMEOUT = "120"
python main.py
```

If a row is still unavailable, the review table includes a **fetch detail** (e.g. `HTTP 403 from CDN`, `opencv-python not installed`) next to the reason.

## Run

```bash
python main.py
```

Open http://localhost:8000

## Input

Excel (`.xlsx` / `.xls`), **CSV** (`.csv`), or JSON with:

- **URL column** (e.g. `creative_url_supplier`) — auto-detected
- **Brand column** — `BRAND`, `brand`, or **`VENDOR_BRAND`** / `vendor_brand` (also matches names like `Vendor Brand`). If none of those exist, grouping uses **`CREATIVE_CAMPAIGN_NAME`** instead.
- **Advertiser column** (e.g. `advertise_name`, `advertiser_name`) — used as subtitle in the UI

CSV files use UTF-8 when possible; semicolon- or comma-separated delimiters are auto-detected.

## Review flow

1. **Unavailable media** (one swipe) — table of rows where no thumbnail could be loaded (hyperlinked creative URL, reason: Unavailable image/video).
2. **Uncertain ads by brand** — outliers, missing brand labels, visual singletons, etc. grouped **by brand** in an image grid (one swipe per brand, not per creative).
3. **Brand groups** — normal fault review grids for grouped creatives.

### Loading hints

While your file downloads, hints from `hint.md` rotate automatically. Each hint stays on screen for about **5–6 seconds** before the next one.

### Uncertain ads (grouped by brand)

Creatives flagged as uncertain are merged into **brand-level grids** so you review every ad for “Nike” in one step instead of scrolling one creative at a time.

- **← / Wrong brand** — brand label does not match this group
- **→ / Correct brand** — label matches
- **Tap an image** to toggle the red **✕ fault** overlay (same as theme-group review below)
- Right-click to inspect media

### Visual subgroups (within a brand)

Theme/similarity groups use the **same large image grid** and per-image **✕ fault** toggle.

When a single brand has many creatives, YAMI splits that brand into **multiple visual subgroups** (colors/themes/layout).

- Each subgroup is still the same **brand**, but only contains **visually similar** creatives.
- Very small clusters (singletons) are sent to **uncertain review** (grouped with other uncertain rows for that brand).

### Brand / theme group review

Wider layout (up to ~1280px) with **larger thumbnails** in the grid (~200–260px cells on desktop).

- **← / Fault** — mark the whole group faulty (swipe)
- **→ / OK** — group OK (swipe)
- **Tap a thumbnail** — toggle **✕** on that creative only (`faultManual` is kept on export)
- Right-click — inspect media

## Output

`isFault`, `advertiserMatch`, and `reviewed` columns are added to the exported `.xlsx`.
