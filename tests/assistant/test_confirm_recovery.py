"""M9: a pending op closed by a `confirm` stays closed across a real DBOS recovery (the closed ops
come from the recorded results, never from state a step mutated). Real process death in a child,
a second child recovers the turn; scripted model, no network."""
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path

from evowiki.assistant.pending import args_hash
from evowiki.assistant.pg_store import PgSessionStore
from evowiki.content.models import Lang
from evowiki.store.users import UserRow, create_user
from tests.assistant.fakes import INFO, T0

ROOT = Path(__file__).resolve().parents[2]
OP = {"pending_id": "po_m0_1", "session_id": "s1",
      "proposed_in": {"message_id": "a0", "session_id": "s1", "role": "assistant"},
      "kind": "call", "tool": "drop_card", "args": {"card_id": "k1"},
      "args_hash": args_hash("drop_card", {"card_id": "k1"}), "title": "不再出这张卡",
      "scope": {"scope_type": "global", "scope_id": None}, "reasked": False,
      "expires_after": None}


def run_child(mode: str, db: str, log: Path) -> int:
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    return subprocess.run([sys.executable, "-m", "tests.assistant.confirm_crash_child", mode, db,
                           str(log)], cwd=ROOT, env=env, timeout=240, check=False).returncode


def test_an_op_confirmed_before_a_crash_is_closed_after_recovery(db_url, pool, tmp_path):
    with pool.connection() as conn:
        create_user(conn, UserRow(user_id="u1", background="none", condition="S2",
                                  lang=Lang.ZH, created_at=T0))
    store = PgSessionStore(pool)
    store.create(INFO)
    store.append("s1", "e1", "user", {"message_id": "m0", "text": "把这张卡删了", "selection": None},
                 T0)
    store.append("s1", "e2", "assistant", {
        "message_id": "a0", "text": "要我不再出这张卡吗？", "sources": [], "proposals": [],
        "actions": [], "tool_calls": 1, "llm_calls": 1, "pending_ops": [OP],
        "skills": {"carry": ["assess"], "refs": [], "versions": {}}}, T0)
    log = tmp_path / "log"
    assert run_child("crash", db_url, log) == 1
    assert run_child("resume", db_url, log) == 0
    ls = log.read_text(encoding="utf-8").splitlines()
    assert "crash confirm:2" in ls and any(ln.startswith("result ") for ln in ls)
    assert Counter(ls)["tool drop_card"] == 1                 # the op ran once, never again
    tools = [e.payload for e in store.entries("s1") if e.type == "tool"
             and e.payload["name"] == "confirm"]
    assert [t["ok"] for t in tools] == [True, False]          # the second confirm: closed
    assert "没有等他确认的操作" in tools[1]["content"]
