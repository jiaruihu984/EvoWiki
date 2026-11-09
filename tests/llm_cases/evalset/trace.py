"""The full trail of one trial, from what the product already records: agent_run / agent_step + llm_text
(migrations 0064, 0090), llm_request, llm_call, llm_attempt, usage_record. A trial owns its user id, so
everything it set off (the turn, the wrap-up, compaction, memory consolidation, a page run, a Bot job)
is found by that user and chained by run id; no second log is kept.

One gzip file per trial: <traces>/<run>/<category>/<case>--<trial>.json.gz, with the case, the record
(state before / after, ledger), every grading check with its basis, and the trail. `missing()` lists
what is absent; a run with anything missing is marked incomplete, never quietly continued.

API keys: `scrub_check` fails a trial's file if any key from the env file appears in it. A material's
text is kept in traces as its first 200 characters + source id (`agent.trace.DOC_TRACE_CHARS`); the
trial's `steps` carry the file's sha256, and `source_page_read` rows carry each page read, so the content
is locatable."""

import gzip
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import psycopg

from evowiki.store.trace import steps as stored_steps
from tests.llm_cases.evalset.spec import DATA, SEALED

SECRET_ENV_SUFFIXES = ("_KEY", "_TOKEN", "_SECRET", "_PASSWORD")


def root(*, sealed: bool = False) -> Path:
    """Sealed trails live with the sealed set; development agents and the improver never read there."""
    return (SEALED if sealed else DATA) / "traces"


def path_of(run_id: str, category: str, case_id: str, trial: int, *, sealed: bool = False) -> Path:
    return root(sealed=sealed) / run_id / category / f"{case_id}--{trial}.json.gz"


def _rows(conn: psycopg.Connection, sql: str, args: tuple) -> list[dict]:
    cur = conn.execute(sql, args)
    names = [c.name for c in cur.description]
    return [dict(zip(names, r, strict=True)) for r in cur.fetchall()]


def collect(conn: psycopg.Connection, user: str) -> dict[str, Any]:
    """Everything the product recorded for this user, full texts resolved."""
    runs = _rows(conn, "select * from agent_run where user_id=%s order by started_at", (user,))
    for r in runs:
        r["steps"] = stored_steps(conn, r["run_id"])
    calls = _rows(conn, "select * from llm_call where user_id=%s order by ts", (user,))
    hashes = [c["input_hash"] for c in calls]
    attempts = _rows(conn, "select * from llm_attempt where input_hash = any(%s) "
                           "order by ts", (hashes,))
    usage = _rows(conn, "select * from usage_record where user_id=%s order by ts", (user,))
    sessions = _rows(conn, "select * from assistant_session where user_id=%s", (user,))
    entries = _rows(conn, "select session_id, seq, type, payload, occurred_at from assistant_entry "
                          "where session_id = any(%s) order by session_id, seq",
                    ([s["session_id"] for s in sessions],))
    actions = _rows(conn, "select * from agent_action where user_id=%s order by created_at", (user,))
    reads = _rows(conn, "select * from source_page_read where user_id=%s", (user,))
    sources = _rows(conn, "select source_id, title, status, kind, text_first, created_at from source "
                          "where user_id=%s", (user,))
    return {"runs": runs, "llm_calls": calls, "llm_attempts": attempts, "usage": usage,
            "sessions": sessions, "entries": entries, "actions": actions, "source_page_reads": reads,
            "sources": sources}


def missing(bundle: dict[str, Any]) -> list[str]:
    """What the trail lacks. Empty = complete: each paid call has its input, output, model, params,
    tokens, cost and time, and sits in a run; each tool call has its args and result; the case, the
    trial, the state on both sides and every check's basis are there."""
    out: list[str] = []
    for k in ("case_id", "trial", "user", "commit"):
        if bundle.get(k) in (None, ""):
            out.append(f"no {k}")
    rec = bundle.get("record") or {}
    if rec.get("harness_error"):
        out.append(f"the harness did not finish: {rec['harness_error'][:120]}")
    elif "before" not in rec or "after" not in rec:
        out.append("no state before / after")
    checks = bundle.get("checks")
    if not checks or any(not c.get("basis") for c in checks):
        out.append("a check has no basis")
    trail = bundle.get("trail") or {}
    steps_by_call: dict[str, dict] = {}
    for r in trail.get("runs", ()):
        for s in r["steps"]:
            if s.get("call_id") and s["kind"] in ("model", "call"):   # a 'reject' step names the same call
                steps_by_call[s["call_id"]] = {**s, "run": r["run_id"]}
            if s["kind"] == "tool" and ("args" not in s or "result" not in s):
                out.append(f"tool step without args or result: {r['run_id']}#{s['seq']}")
    usage_calls = {u.get("call_id") for u in trail.get("usage", ())}
    for c in trail.get("llm_calls", ()):
        cid = c["id"]
        if c.get("served_from"):
            continue                 # answered from the call log: its source row is the paid one
        s = steps_by_call.get(cid)
        if not s:
            out.append(f"llm_call {cid} ({c['purpose']}) has no step: its input is not recoverable")
        elif not s.get("system") and not s.get("prompt") and not s.get("messages"):
            out.append(f"llm_call {cid} ({c['purpose']}): the step holds no input")
        elif s["kind"] == "model" and s.get("turn") and not s["turn"].get("stop_reason"):
            out.append(f"llm_call {cid} ({c['purpose']}): no stop reason")
        if not c.get("run_id"):
            out.append(f"llm_call {cid} ({c['purpose']}) is in no run")
        if c.get("raw_output") in (None, ""):
            out.append(f"llm_call {cid} ({c['purpose']}) has no output")
        if c.get("cost") is None:
            out.append(f"llm_call {cid} ({c['purpose']}) has no cost")
        if cid not in usage_calls:
            out.append(f"llm_call {cid} ({c['purpose']}) has no usage row (tokens)")
    for u in trail.get("usage", ()):
        if not u.get("call_id") and not u.get("replayed") and not _failed_step_for(trail, u):
            out.append(f"usage row {u.get('id')} ({u['purpose']}) matches no call")
    return out


def _failed_step_for(trail: dict, usage: dict) -> bool:
    """A paid failure has a usage row and no llm_call; a failed step or attempt must account for it."""
    return (any(a.get("status") != "accepted" and a["purpose"] == usage["purpose"]
                for a in trail.get("llm_attempts", ()))
            or any(s.get("error") and s["kind"] in ("model", "call") and s.get("purpose") == usage["purpose"]
                   for r in trail.get("runs", ()) for s in r["steps"]))


def secrets(env: dict[str, str]) -> list[str]:
    return [v for k, v in env.items() if k.endswith(SECRET_ENV_SUFFIXES) and len(v) >= 12]


def scrub_check(text: str, keys: Iterable[str]) -> None:
    for k in keys:
        if k in text:
            raise RuntimeError("an API key reached a trace; the file was not written")


def write(path: Path, bundle: dict[str, Any], keys: Iterable[str]) -> int:
    text = json.dumps(bundle, ensure_ascii=False, default=str)
    scrub_check(text, keys)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = gzip.compress(text.encode("utf-8"), compresslevel=6)
    path.write_bytes(data)
    return len(data)


def read(path: Path) -> dict[str, Any]:
    return json.loads(gzip.decompress(path.read_bytes()).decode("utf-8"))


# ---- reading one by hand
def _cut(s: Any, n: int) -> str:
    s = s if isinstance(s, str) else json.dumps(s, ensure_ascii=False, default=str)
    return s if n <= 0 or len(s) <= n else f"{s[:n]}…（共 {len(s)} 字）"


def show(bundle: dict[str, Any], *, full: bool = False) -> str:
    """A transcript a person reads: the case, the verdicts, then each run in time order with every
    model call and tool call. `full`: nothing cut (system prompts run to tens of kilobytes)."""
    n = 0 if full else 1500
    r = bundle["record"]
    lines = [f"# {bundle['case_id']} 第 {bundle['trial']} 次（{bundle['category']} / {bundle['kind']}）",
             f"run {bundle['run_id']} · commit {bundle['commit']} · user {bundle['user']}",
             f"来源：{json.dumps(bundle['source'], ensure_ascii=False)}",
             f"注入：{r.get('fault')}", "", "## 判定"]
    lines += [f"- {'过' if c['ok'] else '没过'} {c['grader']}：{c['basis']}" for c in bundle["checks"]]
    lines += ["", "## 他做了什么，它答了什么"]
    for t in r.get("turns", ()):
        lines += [f"\n他：{t['say']}", f"工具：{[(x['name'], x['ok'], x['status']) for x in t['tools']]}",
                  f"它（{t['seconds']} 秒）：{t['answer']}"]
    lines += [f"\n决定：{json.dumps(r.get('decisions', ()), ensure_ascii=False, default=str)[:1200]}", "",
              "## 状态差异"]
    lines += _state_diff(r)
    lines += ["", "## 轨迹（agent_run → agent_step）"]
    calls = {c["id"]: c for c in bundle["trail"]["llm_calls"]}
    usage = {u.get("call_id"): u for u in bundle["trail"]["usage"]}
    for run in bundle["trail"]["runs"]:
        lines.append(f"\n### run {run['run_id']} · {run['agent']} / {run.get('step_key')} · {run['started_at']} "
                     f"→ stop={run['stop']} err={run['error']}")
        for s in run["steps"]:
            lines.extend(_step(s, calls, usage, n))
    attempts = [a for a in bundle["trail"]["llm_attempts"] if a.get("status") != "accepted"]
    if attempts:
        lines += ["", "## 没被接受的调用尝试"]
        lines += [f"- {a['purpose']} {a['status']} {a.get('error_type')}: {_cut(a.get('error_message') or a.get('raw_output'), 300)}"
                  for a in attempts]
    return "\n".join(lines)


def _step(s: dict, calls: dict, usage: dict, n: int) -> list[str]:
    head = f"\n[{s['seq']}] {s['kind']} {s.get('ts')}"
    if s["kind"] == "tool":
        return [f"{head} {s.get('name')}", f"参数：{_cut(s.get('args'), n)}", f"返回：{_cut(s.get('result'), n)}"]
    if s["kind"] == "reject":
        return [f"{head} 退回：{s.get('reason')} {_cut(s.get('detail'), 300)}"]
    c, u = calls.get(s.get("call_id")) or {}, usage.get(s.get("call_id")) or {}
    meta = (f"{c.get('provider')}/{c.get('model')} v={s.get('prompt_version')} purpose={s.get('purpose') or c.get('purpose')} "
            f"参数={json.dumps(s.get('params') or c.get('params'), ensure_ascii=False)} 耗时 {c.get('latency')} 花费 {c.get('cost')} "
            f"tokens 入 {u.get('input_tokens')} 缓存读 {u.get('cache_read_tokens')} 写 {u.get('cache_write_tokens')} 出 {u.get('output_tokens')}")
    out = [head, meta]
    if s.get("system") is not None:
        out.append(f"system：{_cut(s['system'], n)}")
    if s.get("tools"):
        out.append(f"工具定义 {len(s['tools'])} 个：{[t.get('name') for t in s['tools']]}")
    if s.get("messages"):
        out.append(f"messages（{len(s['messages'])} 条）：{_cut(s['messages'], n)}")
    if s.get("prompt"):
        out.append(f"prompt：{_cut(s['prompt'], n)}")
    out.append(f"回复：{_cut(c.get('raw_output') or s.get('turn'), n)}")
    if s.get("error"):
        out.append(f"错误：{s['error']}")
    return out


def _state_diff(rec: dict) -> list[str]:
    if "before" not in rec or "after" not in rec:
        return ["（没有状态）"]
    b, a = rec["before"], rec["after"]
    out = [f"{d}: 变了" for d in ("graph", "paths", "maps", "settings", "hidden_cards", "review_postponed", "guidelines")
           if b["hash"][d] != a["hash"][d]]
    for pid, p in a["pages"].items():
        old = b["pages"].get(pid)
        if old is None:
            out.append(f"新页 {p['title']}：{len(p['sections'])} 节，正文 {sum(len(s['body']) for s in p['sections'])} 字")
            continue
        for x, y in zip(old["sections"], p["sections"], strict=False):
            if x["body"] != y["body"] or x["heading"] != y["heading"]:
                out += [f"页《{p['title']}》{x['id']}", f"  前：{x['body'][:500]}", f"  后：{y['body'][:500]}"]
    return out or ["没有变化"]
