"""Load rotating loading hints from hint.md."""

import re
from pathlib import Path

HINT_FILE = Path(__file__).resolve().parent / "hint.md"

# Each loading hint stays visible for this long before rotating (milliseconds).
HINT_ROTATE_SECONDS = 15.5

DEFAULT_HINTS = [
    "Downloading creatives… for YAMI - the No. 1 media inspector",
    "YAMI — the best media inspector tool ever lived.",
    "Media Inspector V2, V3 ? Maybe it works... Behold, YAMI - The chosen one.",
    "Extracting thumbnails from video URLs…",
    "Hang tight — large files take a few minutes.",
    "In a world full of broken ETL pipelines… one inspector rises. - YAMI",
    "❌ PAI Media Inspector V2; ❌ QueryBuilder; ❌ ESOV Tool; ❌ Akkio ETL; ✅ YAMI with 38 tabs open and still faster"
]


def _sanitize(text: str) -> str:
    """Strip control chars that break JSON/UI."""
    return re.sub(r"[\x00-\x1f\x7f]", " ", text).strip()


def load_hints() -> list[str]:
    if not HINT_FILE.exists():
        return list(DEFAULT_HINTS)

    hints: list[str] = []
    skip_prefixes = ("tool\t", "other tools", "publicis", "red²", "forget", "ctr", "the real")

    try:
        text = HINT_FILE.read_text(encoding="utf-8")
    except OSError:
        return list(DEFAULT_HINTS)

    for line in text.splitlines():
        line = line.strip()
        if not line or line.isdigit() or line.endswith(":"):
            continue
        if any(line.lower().startswith(p) for p in skip_prefixes):
            continue

        for quoted in re.findall(r'"([^"]{8,140})"', line):
            h = _sanitize(quoted)
            if h:
                hints.append(h)

        if "|" in line and "\t" in line:
            continue

        if line.startswith(('"', "❌", "✅")):
            cleaned = _sanitize(line.strip('"✅❌ \t'))
            if 12 < len(cleaned) < 140:
                hints.append(cleaned)
        elif 20 < len(line) < 120 and line[0].isupper() and "." in line:
            h = _sanitize(line)
            if h:
                hints.append(h)

    seen: set[str] = set()
    unique: list[str] = []
    for h in hints:
        if h not in seen:
            seen.add(h)
            unique.append(h)

    combined = unique + DEFAULT_HINTS
    return combined[:40] if combined else list(DEFAULT_HINTS)


def hint_rotate_ms() -> int:
    return int(HINT_ROTATE_SECONDS * 1000)
