"""Run records and their comparison. One JSON line per run in the configured data directory's history/runs.jsonl
(ignored local runtime data): commit, model and config, case-set and grader hashes,
per-case results, cost, trace location. `rescored_from` marks a grading of an old run's records.

The report is per category. Nothing here adds categories together: a jump in one must not cover a
regression in another."""

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from tests.llm_cases.evalset.spec import CATEGORIES, DATA

HISTORY = DATA / "history" / "runs.jsonl"


def append(run: dict[str, Any], path: Path = HISTORY) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(run, ensure_ascii=False, default=str) + "\n")


def load(path: Path = HISTORY) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def summarize(cases: dict[str, dict]) -> dict[str, dict[str, Any]]:
    """category → its pass^k count, single-trial rate and the same per kind. `cases`: id → {category,
    kind, trials: [bool], not_run?}."""
    out: dict[str, dict[str, Any]] = {}
    groups: dict[str, list[dict]] = defaultdict(list)
    for c in cases.values():
        groups[c["category"]].append(c)
    for cat, rows in groups.items():
        ran = [r for r in rows if r.get("trials") and not r.get("no_verdict")]
        kinds: dict[str, list[dict]] = defaultdict(list)
        for r in ran:
            kinds[r["kind"]].append(r)
        out[cat] = {
            "cases": len(rows), "ran": len(ran),
            "pass_k": sum(all(r["trials"]) for r in ran),
            "trials": sum(len(r["trials"]) for r in ran), "trial_pass": sum(sum(r["trials"]) for r in ran),
            "not_run": [r["id"] for r in rows if not r.get("trials") or r.get("no_verdict")],
            "by_kind": {k: {"cases": len(v), "pass_k": sum(all(r["trials"]) for r in v)} for k, v in sorted(kinds.items())},
        }
    return out


def table(run: dict[str, Any]) -> str:
    head = (f"run {run['run_id']} · commit {run['commit']} · 模型 {run['config'].get('model')} · "
            f"用例 {run['case_hash']} · 判分 {run['grader_hash']}")
    lines = [head]
    for cat, (name, k) in CATEGORIES.items():
        k = run.get("trials_requested") or k
        s = run["summary"].get(cat)
        if not s:
            continue
        kinds = " · ".join(f"{a} {b['pass_k']}/{b['cases']}" for a, b in s["by_kind"].items())
        lines.append(f"  {name}（{cat}，k={k}）：pass^{k} {s['pass_k']}/{s['ran']}，单次 {s['trial_pass']}/{s['trials']}"
                     f"；{kinds}" + (f"；没跑 {len(s['not_run'])}" if s["not_run"] else ""))
    return "\n".join(lines)


def compare(run: dict[str, Any], previous: dict[str, Any] | None) -> str:
    """Against the previous run, per category: counts, what newly fails, what newly passes. Different
    hashes are named, since then the two runs did not take the same test."""
    if previous is None:
        return "没有上一次运行可比。"
    lines = [f"对比上一次 {previous['run_id']}（commit {previous['commit']}）："]
    if previous["case_hash"] != run["case_hash"]:
        lines.append("  用例集变了：下面的数字不是同一张卷子。")
    if previous["grader_hash"] != run["grader_hash"]:
        lines.append("  判分代码变了：下面的变化可能来自判分。")
    for cat, (name, k) in CATEGORIES.items():
        k = run.get("trials_requested") or k
        a, b = previous["summary"].get(cat), run["summary"].get(cat)
        if not b:
            continue
        was = f"{a['pass_k']}/{a['ran']}" if a else "无"
        lines.append(f"  {name}：pass^{k} {was} → {b['pass_k']}/{b['ran']}")
        new_fail, new_pass = [], []
        for cid, c in run["cases"].items():
            if c["category"] != cat or not c.get("trials"):
                continue
            old = previous["cases"].get(cid)
            if not old or not old.get("trials"):
                continue
            if all(old["trials"]) and not all(c["trials"]):
                new_fail.append(cid)
            if not all(old["trials"]) and all(c["trials"]):
                new_pass.append(cid)
        if new_fail:
            lines.append(f"    新失败：{new_fail}")
        if new_pass:
            lines.append(f"    新通过：{new_pass}")
    return "\n".join(lines)


def side_metrics(records: list[dict]) -> dict[str, Any]:
    """What is not being optimized, seen per category (proposal §9.4.7): answer length, tool calls,
    questions back. A big move here is reported whatever the pass counts did."""
    n = sum(len(r.get("turns", ())) for r in records) or 1
    text = [t["answer"] for r in records for t in r.get("turns", ())]
    return {"turns": n, "answer_chars_mean": round(sum(map(len, text)) / n, 1),
            "tool_calls_per_turn": round(sum(len(t["tools"]) for r in records for t in r.get("turns", ())) / n, 2),
            "ask_back_rate": round(sum(a.rstrip().endswith(("？", "?")) for a in text) / n, 3)}
