# Media Inspector

Group creative ads **by brand** (advertiser name) and swipe-review them. Exports Excel with `isFault` and `advertiserMatch` columns.

## Setup

```bash
source .venv/bin/activate
uv pip install -r requirements.txt
```

Optional: `ffmpeg` for MP4 thumbnail extraction.

## Run

```bash
python main.py
```

Open http://localhost:8000

## Input

Excel or JSON with:

- **URL column** (e.g. `creative_url_supplier`) — auto-detected
- **Advertiser column** (e.g. `advertise_name`, `advertiser_name`) — required

## Grouping

Creatives are grouped by **advertiser name from your spreadsheet**, not by visual K-means.

Ads flagged as **uncertain** (missing advertiser, or visually unlike others in the same brand) are reviewed **one at a time**:

- **← / Wrong brand** — label does not match the creative
- **→ / Correct brand** — label matches

Then review each **brand group** as a grid:

- **← / Fault** — faulty creative(s)
- **→ / OK** — not faulty

## Output

`isFault`, `advertiserMatch`, and `reviewed` columns are added to the exported `.xlsx`.
