"""Record aspect-K outputs on 40 seeded random event streams (tests/belief/aspect_streams.py) under
ASPECT_CONFIG_V1, so θ_infer wiring can be checked bit-identical at the default.

Usage: PYTHONPATH=src python scripts/rsi/make_aspect_golden.py"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from evowiki.wikiapp.llm import ASPECT_CONFIG_V1
from tests.belief.aspect_streams import golden_streams, replay, snapshot

OUT = ROOT / "tests" / "belief" / "data" / "aspect_golden.json"


def main() -> None:
    out = []
    for events in golden_streams():
        k, rejected = replay(ASPECT_CONFIG_V1, events)
        out.append(snapshot(k, events, rejected))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(out)} streams)")


if __name__ == "__main__":
    main()
