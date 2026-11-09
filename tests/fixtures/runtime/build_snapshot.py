"""Build tests/fixtures/runtime/content_snapshot.json from the seed fixtures (deterministic).

Run: uv run python -m tests.fixtures.runtime.build_snapshot
"""

import json
import re
from pathlib import Path

HERE = Path(__file__).parent
FIX = HERE.parent
SOURCE = {
    "id": "src_fixture", "title": "Fixture Notes", "locator": "https://example.org/fixture",
    "license": "CC-BY-4.0", "derivative_allowed": True, "redistribution_allowed": True,
    "commercial_allowed": True, "attribution_required": True, "share_alike": False,
    "license_verified_at": "2026-10-01T00:00:00Z",
}
OBLIGATIONS = {"attribution": ["Fixture Notes (CC BY 4.0)"], "share_alike": False,
               "non_commercial": False}


def _sentences(body: str) -> list[str]:
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", body.strip()) if p.strip()]
    return parts or [body.strip()]


def build() -> dict:
    graph = json.loads((FIX / "seed_graph.json").read_text(encoding="utf-8"))
    segs = json.loads((FIX / "seed_segments.json").read_text(encoding="utf-8"))
    passages, segments = [], []
    for seg in sorted(segs["segments"], key=lambda s: s["id"]):
        if seg["review_status"] not in ("auto_verified", "human_approved"):
            continue
        if seg.get("lang", "en") != "en":
            continue
        pid = f"p_{seg['id']}"
        passages.append({"id": pid, "source_id": SOURCE["id"], "locator": seg["id"],
                         "text": seg["body"]})
        segments.append({
            "id": seg["id"], "concept_id": seg["concept_id"], "kind": seg["kind"],
            "lang": "en", "review_status": seg["review_status"],
            "sentences": [{"text": t, "citations": [pid]} for t in _sentences(seg["body"])],
            "license_obligations": OBLIGATIONS,
        })
    seg_ids = {s["id"] for s in segments}
    items = []
    for q in graph.get("quiz_items", []):
        items.append({**q, "stem": q.get("stem") or f"Fixture question {q['id']}",
                      "answer_key": q.get("answer_key") or "a",
                      "answer_verified_by_human": True})
    return {
        "content_version": {"id": "cv_runtimefixture", "created_at": "2026-10-01T00:00:00Z",
                            "snapshot_ref": "versions/cv_runtimefixture/graph.json"},
        "sources": [SOURCE], "passages": passages, "concepts": graph["concepts"],
        "edges": graph["edges"], "quiz_items": items, "segments": segments,
        "perspectives": [], "connections": [c for c in segs.get("connections", [])
                                            if c["segment_id"] in seg_ids],
    }


if __name__ == "__main__":
    (HERE / "content_snapshot.json").write_text(
        json.dumps(build(), indent=1, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8")
