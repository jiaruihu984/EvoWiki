"""Download TutorBench (huggingface `tutorbench/tutorbench`, CC BY-NC 4.0) for the local live
rubric cases (tests/llm_cases/live/test_live_tutorbench.py). Local use only: the rows land in
tests/llm_cases/data/tutorbench/ (gitignored) with an ATTRIBUTION.md next to them and are never
committed or redistributed. Stdlib only (urllib), through the HF datasets-server rows API.

Usage (ask the user before running: it downloads from the internet):

    uv run python scripts/fetch_tutorbench.py [--out DIR]

Prints the row count, how many rows carry an image (the live case skips them: the assistant is
text-only) and the shape of the first row's RUBRICS, so the first real run shows the format."""

import argparse
import json
import sys
import urllib.request
from pathlib import Path
from typing import Any

DATASET = "tutorbench/tutorbench"
SOURCE_URL = f"https://huggingface.co/datasets/{DATASET}"
ROWS_API = ("https://datasets-server.huggingface.co/rows?dataset=tutorbench%2Ftutorbench"
            "&config=default&split=train&offset={offset}&length={length}")
PAGE = 100                        # the rows API's maximum page length
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "tests" / "llm_cases" / "data" / "tutorbench"
COLUMNS = ("TASK_ID", "SUBJECT", "PROMPT", "UC1_INITIAL_EXPLANATION", "FOLLOW_UP_PROMPT",
           "RUBRICS")
IMAGE_FIELDS = ("Image", "IMAGE_URL")
ATTRIBUTION = f"""# TutorBench (local copy)

Source: {SOURCE_URL}
License: CC BY-NC 4.0 (https://creativecommons.org/licenses/by-nc/4.0/)

Downloaded by scripts/fetch_tutorbench.py for EvoWiki's local live rubric cases.
Local use only, not redistributed: this folder is gitignored and never committed.
"""


class RowsFormatError(ValueError):
    """A rows-API page or row in a shape this script does not know."""


def parse_rows(payload: Any) -> tuple[list[dict[str, Any]], int]:
    """One rows-API page → (the rows' column dicts, num_rows_total). A page that is not the
    rows-API shape, or a row missing a needed column, raises RowsFormatError (never guessed)."""
    if not isinstance(payload, dict) or not isinstance(payload.get("rows"), list):
        raise RowsFormatError("not a datasets-server rows page: no 'rows' list")
    total = payload.get("num_rows_total")
    if not isinstance(total, int):
        raise RowsFormatError("not a datasets-server rows page: no integer 'num_rows_total'")
    rows: list[dict[str, Any]] = []
    for i, r in enumerate(payload["rows"]):
        row = r.get("row") if isinstance(r, dict) else None
        if not isinstance(row, dict):
            raise RowsFormatError(f"rows[{i}] has no 'row' object")
        missing = [c for c in COLUMNS if c not in row]
        if missing:
            raise RowsFormatError(f"rows[{i}] misses columns {missing}")
        rows.append(row)
    return rows, total


def has_image(row: dict[str, Any]) -> bool:
    """A row whose `Image` (or IMAGE_URL) is non-empty: the text-only assistant cannot see it."""
    return any(row.get(f) not in (None, "", [], {}) for f in IMAGE_FIELDS)


def split_rows(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    """(text-only rows, TASK_IDs of the rows with an image)."""
    text = [r for r in rows if not has_image(r)]
    images = [str(r["TASK_ID"]) for r in rows if has_image(r)]
    return text, images


def rubrics_shape(raw: Any) -> str:
    """A one-line description of a RUBRICS value (type, length, first element's keys)."""
    value = raw
    prefix = ""
    if isinstance(raw, str):
        prefix = "str holding "
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            return f"str (not JSON), {len(raw)} chars"
    if isinstance(value, list):
        first = value[0] if value else None
        keys = (sorted(first) if isinstance(first, dict)
                else type(first).__name__ if first is not None else "-")
        inner = (sorted(first["attributes"]) if isinstance(first, dict)
                 and isinstance(first.get("attributes"), dict) else None)
        return (f"{prefix}list of {len(value)}; first element: {keys}"
                + (f"; attributes: {inner}" if inner else ""))
    return f"{prefix}{type(value).__name__}"


def _get(url: str) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "evowiki-fetch-tutorbench"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch(get=_get) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    total = None
    while total is None or len(rows) < total:
        page, total = parse_rows(get(ROWS_API.format(offset=len(rows), length=PAGE)))
        if not page:
            raise RowsFormatError(f"rows API returned an empty page at offset {len(rows)} of {total}")
        rows.extend(page)
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args(argv)
    rows = fetch()
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "rows.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1),
                                        encoding="utf-8")
    (args.out / "ATTRIBUTION.md").write_text(ATTRIBUTION, encoding="utf-8")
    text, images = split_rows(rows)
    print(f"{len(rows)} rows -> {args.out / 'rows.json'}")
    print(f"text-only: {len(text)}; with an image (skipped by the live case): {len(images)}")
    if rows:
        print(f"first row RUBRICS: {rubrics_shape(rows[0]['RUBRICS'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
