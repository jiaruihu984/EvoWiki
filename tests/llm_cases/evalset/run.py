"""The evaluation set's command line. Paid when it runs the cases; the rest is free.

  python -m tests.llm_cases.evalset.run run [--port 8310] [--only id,id] [--category claims] [--note ...]
  python -m tests.llm_cases.evalset.run rescore <run_id>      grade the stored records again, no product call
  python -m tests.llm_cases.evalset.run compare [run_id]      per-category table against the previous run
  python -m tests.llm_cases.evalset.run show <run_id> <case> <trial> [--full]    one transcript, for reading
  python -m tests.llm_cases.evalset.run failures <run_id>     every failed case with its verbatim basis
  python -m tests.llm_cases.evalset.run sealed-init           the sealed folder's skeleton, contents untouched
  python -m tests.llm_cases.evalset.run down                  stop the rig backend

`run` makes the rig database audit_<lane> from audit_base on the isolated test cluster (15433 or 25433) and starts `backend.py` from this checkout. It never reads or writes the dev database."""

import argparse
import json
import os
import random
import secrets as pysecrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

import psycopg
from dotenv import dotenv_values

from tests.llm_cases.evalset import graders, history, spec, trace, worlds
from tests.llm_cases.evalset.spec import CATEGORIES, DATA, SEALED, TEST_PORT

ROOT = Path(__file__).resolve().parents[3]
MAIN_PY = Path(sys.executable)
RIG = DATA / "rig"
LOGS = DATA / "logs"
QUIET_S = 45            # background work (wrap-up, memory, page runs) has this long of silence to end
TRACE_KEEP_RUNS = 20    # `prune`: newest kept; baselines are kept for good


def env_file() -> dict[str, str]:
    path = Path(os.environ.get("EVOWIKI_ENV_FILE") or ROOT / ".env")
    if not path.is_absolute():
        path = ROOT / path
    if not path.is_file():
        raise FileNotFoundError("Evaluation configuration file is missing")
    return {**{key: value for key, value in dotenv_values(path, encoding="utf-8").items()
               if value is not None}, **os.environ}


def admin_dsn(db: str) -> str:
    return f"postgresql://postgres@127.0.0.1:{TEST_PORT}/{db}"


def rig_up(lane: str, port: int) -> dict[str, str]:
    """The rig's recipe (tests/e2e/live_rig.py) with this module as the service: database from
    audit_base, migrate, serve. Empty vault folder: trials write notes for users of their own."""
    env = env_file()
    db = f"audit_{lane}"
    rig_down(lane)
    if _port_taken(port):
        raise SystemExit(f"port {port} is in use (another lane's rig?): pick another with --port")
    rig = rig_dir(lane)
    with psycopg.connect(admin_dsn("postgres"), autocommit=True) as c:
        if not c.execute("select 1 from pg_database where datname=%s", (db,)).fetchone():
            c.execute(f'create database "{db}" template audit_base')
    rig.mkdir(parents=True, exist_ok=True)
    vault = rig / "vault"
    vault.mkdir(exist_ok=True)
    faults = rig / "faults"
    faults.mkdir(exist_ok=True)
    own = admin_dsn(db)
    app = f"postgresql://evowiki_app@127.0.0.1:{TEST_PORT}/{db}"
    if f":{TEST_PORT}/" not in app or "15432" in app:
        raise SystemExit(f"refusing a database outside the test cluster: {db}")
    child = {**os.environ, **env, "EVOWIKI_DATABASE_URL": app, "EVOWIKI_ADMIN_DATABASE_URL": own,
             "PYTHONPATH": f"{ROOT / 'src'}{os.pathsep}{ROOT}", "EVOWIKI_VAULT": str(vault), "PYTHONIOENCODING": "utf-8",
             "EVOWIKI_BOT": "off", "EVOWIKI_EVALSET_FAULTS": str(faults), "EVOWIKI_COMMIT": commit()}
    child.pop("EVOWIKI_OTEL_ENDPOINT", None)
    py = str(MAIN_PY)
    r = subprocess.run([py, "-m", "evowiki.service", "migrate"], env={**child, "EVOWIKI_DATABASE_URL": own},
                       cwd=ROOT, capture_output=True, text=True, errors="replace", check=False)
    if r.returncode != 0:
        raise SystemExit(f"migrate failed:\n{r.stdout}\n{r.stderr}")
    log = (rig / "backend.log").open("ab")
    p = subprocess.Popen([py, "-m", "tests.llm_cases.evalset.backend", "--port", str(port)], env=child, cwd=ROOT,
                         stdout=log, stderr=subprocess.STDOUT,
                         creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS)
    (rig / "state.json").write_text(json.dumps({"pid": p.pid, "port": port, "db": db}), encoding="utf-8")
    for _ in range(150):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health/ready", timeout=2):
                return {"url": f"http://127.0.0.1:{port}", "db": db, "faults": str(faults), "pid": str(p.pid)}
        except (urllib.error.URLError, OSError):
            if p.poll() is not None:
                raise SystemExit(f"backend exited {p.returncode}; see {rig / 'backend.log'}") from None
            time.sleep(2)
    raise SystemExit(f"backend not ready; see {rig / 'backend.log'}")


def rig_dir(lane: str) -> Path:
    return RIG / lane


def _port_taken(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", port)) == 0


def rig_down(lane: str) -> None:
    """This lane's backend, and no other lane's: the processes of the backend module on the port its
    state file names (the venv's python.exe is a trampoline, so the pid Popen returned is not the
    server's; a second backend on one database would take over running workflows through DBOS
    recovery). No state file: nothing of this lane runs."""
    state = rig_dir(lane) / "state.json"
    if not state.exists():
        return
    port = int(json.loads(state.read_text(encoding="utf-8"))["port"])
    script = ("Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -match "
              f"'tests.llm_cases.evalset.backend --port {port}( |$)' }} | ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force "
              "-ErrorAction SilentlyContinue }")
    subprocess.run(["powershell", "-NoProfile", "-Command", script], check=False, capture_output=True)


def commit() -> str:
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
    dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=ROOT, capture_output=True,
                           text=True, check=True).stdout.strip()
    return head + ("+dirty" if dirty else "")


def model_config(env: dict[str, str]) -> dict[str, str]:
    keys = ("EVOWIKI_RUNTIME_PROVIDER", "EVOWIKI_RUNTIME_MODEL", "EVOWIKI_RUNTIME_FAST_MODEL", "EVOWIKI_DEEP_MODEL",
            "EVOWIKI_VISION_MODEL", "EVOWIKI_RUNTIME_EFFORT", "EVOWIKI_RUNTIME_THINK_EFFORT", "EVOWIKI_DEEP_EFFORT")
    return {"model": env.get("EVOWIKI_RUNTIME_MODEL", ""), **{k: env.get(k, "") for k in keys}}


def wait_quiet(conn: psycopg.Connection, prefix: str) -> None:
    """Until nothing of this run's users moved for QUIET_S (a wrap-up, a consolidation, a page run a
    trial started may still be going when the last trial ends)."""
    last, t0 = None, time.monotonic()
    while time.monotonic() - t0 < 900:
        cur = conn.execute("select count(*), max(ts) from llm_call where user_id like %s", (prefix + "%",)).fetchone()
        live = conn.execute("select count(*) from agent_run where user_id like %s and finished_at is null",
                            (prefix + "%",)).fetchone()[0]
        if cur == last and not live:
            return
        if cur != last:
            last = cur
        time.sleep(QUIET_S)


def run_cases(a: argparse.Namespace) -> int:
    if a.candidate:
        os.environ["EVOWIKI_EVALSET_CANDIDATE"] = a.candidate
    from inspect_ai import eval as inspect_eval

    from tests.llm_cases.evalset.judge import Judge, english_anchors
    from tests.llm_cases.evalset.task import RECORD, task_for

    sealed = a.sealed
    all_cases = spec.load(sealed=sealed, language=a.language)
    chosen = [c for c in all_cases
              if (not a.only or c.id in a.only.split(",")) and (not a.category or c.category == a.category)]
    env = env_file()
    if a.model:
        os.environ["EVOWIKI_EVALSET_MODEL"] = a.model
    run_id = datetime.now(UTC).strftime("%m%d%H%M") + pysecrets.token_hex(2)
    rig = rig_up(a.lane, a.port)
    dsn = admin_dsn(rig["db"])
    judge = Judge(env)
    anchors = english_anchors(judge) if a.language == "en" else []
    if any(not row["passed"] for row in anchors):
        print(json.dumps({"error": "English judge controls failed", "anchors": anchors,
                          "judge": judge.spend()}, ensure_ascii=False))
        rig_down(a.lane)
        return 2
    tasks, not_run = [], {}
    for cat in CATEGORIES:
        cs = [c for c in chosen if c.category == cat and not c.missing and not c.fixture]
        not_run.update({c.id: c for c in chosen if c.category == cat and c.missing})
        if cs:
            tasks.append(task_for(cat, cs, base_url=rig["url"], dsn=dsn, faults=Path(rig["faults"]),
                                  run_id=run_id, judge=judge, trials=a.trials))
    t0 = time.monotonic()
    logs_dir = (SEALED / "logs" if sealed else LOGS) / run_id
    logs = inspect_eval(tasks, model="none/none", log_dir=str(logs_dir), max_samples=a.concurrency, max_tasks=a.tasks,
                        display="plain", fail_on_error=False, score_display=False, log_shared=False,
                        metadata={"evalset_run": run_id})
    seconds = round(time.monotonic() - t0, 1)
    with psycopg.connect(dsn, autocommit=True) as conn:
        wait_quiet(conn, f"ev-{run_id}-")
    by = {c.id: c for c in all_cases}
    rec = finish(run_id, logs, by, not_run, dsn, env, judge, seconds, a, sealed, RECORD, anchors)
    print(history.table(rec) if not sealed else _bit(rec))
    prev = [r for r in history.load(_hist(sealed)) if r["run_id"] != run_id and r.get("complete_case_set")]
    if not sealed:
        print(history.compare(rec, prev[-1] if prev else None))
    print(f"轨迹完整：{rec['trace']['complete']}，{rec['trace']['files']} 个文件，{rec['trace']['bytes'] / 1e6:.1f} MB，"
          f"缺项 {len(rec['trace']['missing'])}")
    print(f"花费：产品 ${rec['cost']['product_usd']:.4f}（{rec['cost']['calls']} 次调用，{seconds} 秒），"
          f"裁判 ${rec['cost']['judge']['cost_usd']:.4f}（{rec['cost']['judge']['calls']} 次）")
    if not a.keep_up:
        rig_down(a.lane)
    return 0 if rec["trace"]["complete"] else 3


def _hist(sealed: bool) -> Path:
    if os.environ.get("EVOWIKI_EVALSET_CANDIDATE"):
        return history.HISTORY.parent / "candidates.jsonl"
    return SEALED / "history" / "runs.jsonl" if sealed else history.HISTORY


def _bit(rec: dict) -> str:
    return "\n".join(f"{CATEGORIES[c][0]}：{'通过' if s['pass_k'] == s['ran'] else '不通过'}" for c, s in rec["summary"].items())


def finish(run_id, logs, by, not_run, dsn, env, judge, seconds, a, sealed, record_key, anchors) -> dict:
    cases: dict[str, dict] = {c.id: {"id": c.id, "category": c.category, "kind": c.kind, "trials": [], "not_run": c.missing}
                              for c in not_run.values()}
    records_by_cat: dict[str, list] = {}
    missing: list[dict] = []
    files = size = 0
    keys = trace.secrets(env)
    cmt = commit()
    with psycopg.connect(dsn, autocommit=True) as conn:
        for log in logs:
            for s in log.samples or ():
                case = by[s.id]
                row = cases.setdefault(s.id, {"id": s.id, "category": case.category, "kind": case.kind, "trials": [],
                                              "failures": [], "users": [],
                                              "say": [st["say"] for st in case.steps if st.get("say")]})
                r = s.store.get(record_key)
                if r is None:
                    row["no_verdict"] = True
                    missing.append({"case": s.id, "trial": s.epoch, "missing": [f"no record: {s.error}"]})
                    continue
                records_by_cat.setdefault(case.category, []).append(r)
                if not s.scores:
                    row["no_verdict"] = True
                    missing.append({"case": s.id, "trial": s.epoch, "missing": [f"no score: {s.error}"]})
                    continue
                sc = next(iter(s.scores.values()))
                ok = sc.value == "C"
                row["trials"].append(ok)
                row["users"].append(r["user"])
                undecided = sc.metadata.get("undecided")
                if undecided:
                    row["no_verdict"] = True
                if not ok:
                    row["failures"].append({"trial": s.epoch, "basis": sc.explanation, "answer": graders.last_answer(r)[:600],
                                            "undecided": undecided})
                bundle = {"run_id": run_id, "case_id": s.id, "trial": s.epoch, "category": case.category, "kind": case.kind,
                          "user": r["user"], "commit": cmt, "source": case.source, "case_grade": case.grade,
                          "record": r, "checks": sc.metadata["checks"], "trail": trace.collect(conn, r["user"])}
                miss = trace.missing(bundle)
                if miss:
                    missing.append({"case": s.id, "trial": s.epoch, "missing": miss[:8]})
                size += trace.write(trace.path_of(run_id, case.category, s.id, s.epoch, sealed=sealed), bundle, keys)
                files += 1
        prefix = f"ev-{run_id}-"
        calls, cost, lat = conn.execute("select count(*), coalesce(sum(cost),0), coalesce(sum(latency),0) from llm_call "
                                        "where user_id like %s and served_from is null", (prefix + "%",)).fetchone()
        paid = conn.execute("select coalesce(sum(cost_usd),0), count(*) from usage_record where user_id like %s and not replayed",
                            (prefix + "%",)).fetchone()
        by_purpose = conn.execute("select purpose, count(*), round(sum(cost_usd)::numeric,5) from usage_record where user_id like %s "
                                  "and not replayed group by 1 order by 3 desc nulls last", (prefix + "%",)).fetchall()
        models = conn.execute("select provider, model, count(*) from llm_call where user_id like %s group by 1,2", (prefix + "%",)).fetchall()
        harness_rounds = conn.execute("select count(*) from harness_round where user_id like %s", (prefix + "%",)).fetchone()[0]
        no_user = conn.execute("select count(*) from llm_call where user_id is null and ts > now() - make_interval(secs => %s)",
                               (seconds + 1200,)).fetchone()[0]
    if no_user:
        missing.append({"case": "*", "trial": 0, "missing": [f"{no_user} llm_call rows with no user during the run"]})
    all_ran = not a.only and not a.category
    rec = {"run_id": run_id, "at": datetime.now(UTC).isoformat(), "commit": cmt, "config": model_config(env),
           "language": a.language, "selected_model": a.model, "trials_requested": a.trials,
           "judge_anchors": anchors,
           "models_used": [list(m) for m in models], "case_hash": spec.case_hash(list(by.values()), worlds.file_hash),
           "grader_hash": spec.grader_hash(), "complete_case_set": all_ran, "note": a.note, "cases": cases,
           "summary": history.summarize(cases), "side": {k: history.side_metrics(v) for k, v in records_by_cat.items()},
           "cost": {"product_usd": float(paid[0]), "usage_rows": paid[1], "calls": calls, "llm_call_cost": float(cost),
                    "llm_seconds": round(float(lat), 1), "seconds": seconds,
                    "by_purpose": [[p, n, float(c or 0)] for p, n, c in by_purpose], "judge": judge.spend()},
           "harness_rounds": harness_rounds,
           "logs": [str(x.location) for x in logs],
           "trace": {"dir": str(trace.root(sealed=sealed) / run_id), "files": files, "bytes": size,
                     "missing": missing, "complete": not missing}}
    history.append(rec, _hist(sealed))
    return rec


def rescore(a: argparse.Namespace) -> int:
    """Records → graders, from the trace files. No backend, no product model. A judge question that
    is new costs a judge call; an old one comes from the cache."""
    from tests.llm_cases.evalset.judge import Judge

    runs = history.load()
    old = next(r for r in runs if r["run_id"] == a.run_id)
    by = {c.id: c for c in spec.load(language=old.get("language", "zh"))}
    judge = Judge(env_file())
    cases: dict[str, dict] = {}
    for p in sorted(Path(old["trace"]["dir"]).rglob("*.json.gz")):
        b = trace.read(p)
        case = by.get(b["case_id"])
        if case is None:
            continue
        checks = graders.grade(case, b["record"], judge)
        row = cases.setdefault(case.id, {"id": case.id, "category": case.category, "kind": case.kind, "trials": [], "failures": []})
        row["trials"].append(graders.passed(checks))
        if not row["trials"][-1]:
            row["failures"].append({"trial": b["trial"], "basis": "; ".join(c.basis for c in checks if not c.ok),
                                    "answer": graders.last_answer(b["record"])[:600]})
    rec = {**old, "run_id": f"{old['run_id']}r{pysecrets.token_hex(1)}", "rescored_from": old["run_id"], "cases": cases,
           "summary": history.summarize(cases), "case_hash": spec.case_hash(list(by.values()), worlds.file_hash),
           "grader_hash": spec.grader_hash(), "at": datetime.now(UTC).isoformat(),
           "cost": {**old["cost"], "judge": judge.spend(), "rescore": True}}
    history.append(rec)
    print(history.table(rec))
    print(history.compare(rec, old))
    print(f"重新判分：产品调用 0 次，裁判 {judge.spend()['calls']} 次 ${judge.spend()['cost_usd']:.4f}")
    return 0


def compare(a: argparse.Namespace) -> int:
    runs = history.load()
    i = next((n for n, r in enumerate(runs) if r["run_id"] == a.run_id), len(runs) - 1) if a.run_id else len(runs) - 1
    print(history.table(runs[i]))
    print(history.compare(runs[i], runs[i - 1] if i > 0 else None))
    return 0


def failures(a: argparse.Namespace) -> int:
    run = next(r for r in history.load() if r["run_id"] == a.run_id)
    for cid, c in run["cases"].items():
        if c.get("trials") and all(c["trials"]):
            continue
        print(f"\n== {c['category']} / {c['kind']} / {cid}：{c['trials']} {c.get('not_run') or ''}")
        for f in c.get("failures", ()):
            print(f"  第 {f['trial']} 次：{f['basis'][:500]}")
            print(f"    答：{f['answer'][:300]!r}")
    return 0


def show(a: argparse.Namespace) -> int:
    run = next(r for r in history.load() if r["run_id"] == a.run_id)
    if a.case == "random":
        files = sorted(Path(run["trace"]["dir"]).rglob("*.json.gz"))
        p = random.choice(files)
    else:
        p = next(Path(run["trace"]["dir"]).rglob(f"{a.case}--{a.trial}.json.gz"))
    print(trace.show(trace.read(p), full=a.full))
    return 0


SEALED_README = """# 密封集（不进仓库）

此目录存放用户明确授权的评测资料。开发 agent 和改进器都不读这个目录；抽样由代码做
（`tests/llm_cases/evalset/spec.py is_sealed_unit`：主题加失败来源的哈希，一个单位不跨层）。

cases/<类别>.yaml   和 tests/llm_cases/evalset/cases 同样的格式。只有第 1、2、3、5 类，且一类不足 20 条不设。
local/<名字>.yaml   用例引用的讲义路径和标准答案
data/               讲义、对话原文
traces/ logs/ history/   运行产物。运行只向外说每类通过或不通过，不说是哪条。
"""


def sealed_init(_: argparse.Namespace) -> int:
    for d in ("cases", "local", "data", "traces", "logs", "history"):
        (SEALED / d).mkdir(parents=True, exist_ok=True)
    (SEALED / "README.md").write_text(SEALED_README, encoding="utf-8")
    print(f"{SEALED}: 目录结构已建，内容为空")
    return 0


def prune(a: argparse.Namespace) -> int:
    """Traces: the newest TRACE_KEEP_RUNS runs and every run with a note kept; older ones deleted.
    The history line stays, so results outlive their trails."""
    runs = [r for r in history.load() if "rescored_from" not in r]
    keep = {r["run_id"] for r in runs[-TRACE_KEEP_RUNS:]} | {r["run_id"] for r in runs if r.get("note")}
    n = 0
    for r in runs:
        d = Path(r["trace"]["dir"])
        if r["run_id"] not in keep and d.exists():
            shutil.rmtree(d)
            n += 1
    print(f"删了 {n} 次运行的轨迹，保留 {len(keep)} 次")
    return 0


def fixtures(a: argparse.Namespace) -> int:
    """The cases that grade stored output (no trial, no model): one line per case, exit 1 on a failure."""
    from tests.llm_cases.evalset import fixtures as fx
    return fx.run_all(sealed=a.sealed)


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--port", type=int, default=8310)
    r.add_argument("--lane", default="evalset")
    r.add_argument("--only", default="")
    r.add_argument("--category", default="")
    r.add_argument("--language", choices=("zh", "en"), default="zh")
    r.add_argument("--model", default="", help="explicit configured provider/model identifier")
    r.add_argument("--trials", type=int, choices=range(1, 11), default=None)
    r.add_argument("--concurrency", type=int, default=6, help="trials at once per category")
    r.add_argument("--tasks", type=int, default=3, help="categories at once; all 8 at 6 each (48 trials) got HTTP 429s")
    r.add_argument("--note", default="")
    r.add_argument("--sealed", action="store_true")
    r.add_argument("--keep-up", action="store_true")
    r.add_argument("--candidate", default="", help="a harness diff every trial's user runs; its record goes to candidates.jsonl")
    s = sub.add_parser("rescore")
    s.add_argument("run_id")
    c = sub.add_parser("compare")
    c.add_argument("run_id", nargs="?")
    f = sub.add_parser("failures")
    f.add_argument("run_id")
    w = sub.add_parser("show")
    w.add_argument("run_id")
    w.add_argument("case")
    w.add_argument("trial", nargs="?", type=int, default=1)
    w.add_argument("--full", action="store_true")
    sub.add_parser("sealed-init")
    sub.add_parser("fixtures").add_argument("--sealed", action="store_true")
    sub.add_parser("seal")
    sub.add_parser("prune")
    sub.add_parser("down").add_argument("--lane", default="evalset")
    a = ap.parse_args()
    return {"run": run_cases, "rescore": rescore, "compare": compare, "failures": failures, "show": show,
            "sealed-init": sealed_init, "fixtures": fixtures, "seal": lambda _: __import__(
                "tests.llm_cases.evalset.seal", fromlist=["x"]).run(), "prune": prune, "down": lambda x: rig_down(x.lane) or 0}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
