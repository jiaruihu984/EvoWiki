"""Stage 3 on Postgres with the real ledger and the real action tools (M11): load → gate → a draft
bound for 「好」 →
「好」 → confirm through `run_turn` (scripted model, no paid call), and the database pieces S3
added: `ActionLedger.add_result_keys`, `ProfileFactExecutor`, `PgProfileStore.update_in`."""

import re

import pytest

from evowiki.assistant.contracts import MessageIn
from evowiki.assistant.loop import run_turn
from evowiki.assistant.session import fold
from evowiki.assistant.tools.actions import ActionDeps, RememberAboutMeTool, build_action_tools
from evowiki.bot.executors_profile import KIND as PROFILE_FACT
from evowiki.bot.executors_profile import ProfileFactExecutor
from evowiki.llm.chat import ChatTurn, ToolCall
from evowiki.llm.scripted import ScriptedChatProvider
from evowiki.profile.note import Profile
from evowiki.profile.store import PgProfileStore
from evowiki.wikiapp.path_order import shown
from tests.assistant.test_action_tools import CTX as _CTX
from tests.assistant.test_action_tools import INFO, P2, PATH_ID, T0, env, turn_deps  # noqa: F401

ACT = re.compile(r"act_[0-9A-Za-z_]+")


def _script(*steps):
    """Scripted turns; a callable step is given the request (it reads ids from tool results)."""
    queue = list(steps)

    def chat(_model, req):
        out = queue.pop(0)
        return out(req) if callable(out) else out
    return ScriptedChatProvider("scripted", chat)


def _call(name, i, **args):
    return ChatTurn(tool_calls=(ToolCall(id=f"c{i}", name=name, args=args),))


def _confirm_the_op(req):
    pid = re.findall(r"po_act_[0-9A-Za-z_]+", req.messages[-1].text)[-1]
    return _call("confirm", 4, pending_id=pid, asked="好")


def test_load_gate_and_ok_run_a_real_split_page_draft(tmp_path, env):  # noqa: F811
    before = [i.key for i in shown(env.state().paths[PATH_ID])]
    prov = _script(_call("split_page", 1, page=P2, parts=[{"title": "甲"}, {"title": "乙"}]),
                   _call("read_ref", 2, path="edit-wiki/path"),
                   _call("split_page", 3, page=P2, parts=[{"title": "甲"}, {"title": "乙"}],
                         why="太笼统"),
                   ChatTurn(text="草稿在卡片上，回「好」就拆。"),
                   _confirm_the_op, ChatTurn(text="拆好了。"))
    deps = turn_deps(tmp_path, env, prov, build_action_tools(env.deps))
    list(run_turn(deps, INFO, MessageIn(text="这一页太笼统了，帮我拆解一下")))
    st = fold(INFO, deps.store.entries("s1"))
    gated = [e.payload for e in deps.store.entries("s1")
             if e.type == "tool" and e.payload["name"] == "split_page"]
    assert not gated[0]["ok"] and "先 read_ref edit-wiki/path" in gated[0]["content"]
    [op] = st.turns[-1].pending_ops
    row = env.ledger.get("u1", op["action_id"])
    assert row.status == "proposed"
    assert [i.key for i in shown(env.state().paths[PATH_ID])] == before   # nothing changed yet
    list(run_turn(deps, INFO, MessageIn(text="好")))
    row = env.ledger.get("u1", op["action_id"])
    assert row.status == "executed", row.detail
    assert [e["role"] for e in row.result["confirmed_by"]] == ["assistant", "user"]
    assert [i.key for i in shown(env.state().paths[PATH_ID])] != before


def test_add_result_keys_merges_into_the_result_and_keeps_the_content_hash(env):  # noqa: F811
    res = env.run("split_page", page=P2, parts=[{"title": "甲"}, {"title": "乙"}])
    aid = res.action.action_id
    before = env.ledger.get("u1", aid)
    assert env.ledger.add_result_keys("u1", aid, {"confirmed_by": [{"role": "user"}]})
    after = env.ledger.get("u1", aid)
    assert after.result["confirmed_by"] == [{"role": "user"}]
    assert after.content_hash == before.content_hash and after.content == before.content
    assert not env.ledger.add_result_keys("u2", aid, {"x": 1})          # another user's row
    assert not env.ledger.add_result_keys("u1", "act_nope", {"x": 1})


def test_profile_fact_rows_write_the_profile_and_undo_it(env, pool):  # noqa: F811
    store = PgProfileStore(pool, lambda: T0)
    env.ledger.register(PROFILE_FACT, ProfileFactExecutor(store, pool))
    tool = RememberAboutMeTool(ActionDeps(repo=env.repo, ledger=env.ledger, clock=lambda: T0,
                                          new_id=env.ids))
    ctx = _CTX.model_copy(update={"current_words": ("我是护士，三年了",),
                                  "user_words": ("我是护士，三年了",), "message_id": "m1"})
    res = tool.run({"applies": "lasting", "section": "我是谁", "quote": "我是护士", "text": "护士"},
                   ctx)
    assert res.error is None, res.content
    assert store.get("u1").owned["我是谁"] == ("护士",)
    meta = next(iter(store.get("u1").fact_meta.values()))     # where it came from (T6)
    assert (meta.provenance, meta.origin_ref) == ("explicit_current", f"{ctx.session_id}/m1")
    row = env.ledger.get("u1", res.action.action_id)
    assert (row.kind, row.status, row.level) == (PROFILE_FACT, "executed", "auto")
    undone = env.ledger.undo("u1", row.action_id)
    assert undone.status == "undone" and store.get("u1").owned.get("我是谁", ()) == ()


def test_update_in_commits_with_the_callers_transaction(pool):
    from tests.runtime.helpers import make_user
    make_user(pool, "u9")
    store = PgProfileStore(pool, lambda: T0)
    with pytest.raises(RuntimeError), pool.connection() as conn, conn.transaction():
        store.update_in(conn, "u9", lambda p: Profile(owned={"我是谁": ("护士",)}))
        raise RuntimeError("the caller's commit fails")
    assert store.get("u9") == Profile()                    # rolled back with the caller
    with pool.connection() as conn, conn.transaction():
        store.update_in(conn, "u9", lambda p: Profile(owned={"我是谁": ("护士",)}))
    assert store.get("u9").owned == {"我是谁": ("护士",)}
