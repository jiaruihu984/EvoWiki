"""What the learner says in the chat is kept, with where it came from (stage 3 T6 / T7; rule
provenance contract §5 / §7 #4 / #5 / #10 / #11; memory spec §2.3, §4.2): `remember_preference` /
`remember_about_me` and Memory's backfill land through `wikiapp.preferences.land_preference`; a
complaint becomes a `user_complaint` signal (`report_problem`). Real Postgres, the real ledger +
executors, a SCRIPTED chat model - nothing here calls a paid API."""

from dataclasses import replace as dc_replace

import pytest

from evowiki.agent.tools import ToolContext
from evowiki.assistant.contracts import MessageIn
from evowiki.assistant.loop import run_turn
from evowiki.assistant.session import MemorySessionStore, SessionInfo, fold, session_out
from evowiki.assistant.skills import library
from evowiki.assistant.tools.actions import (
    ALREADY_IN_FORCE,
    NOT_QUOTED_ANY,
    THIS_TASK,
    restyle_candidates,
)
from evowiki.assistant.wiki import WikiData
from evowiki.bot.executors_profile import ProfileFactExecutor
from evowiki.deep.tools import preread_spec
from evowiki.llm.chat import ChatTurn, ToolCall
from evowiki.memory.contracts import EvidenceRef, MemoryScope
from evowiki.profile.context import context_for
from evowiki.profile.derive import derived
from evowiki.profile.note import HOW, WHO, line_key
from evowiki.profile.store import PgProfileStore
from evowiki.wikiapp import teachspec as tspec
from evowiki.wikiapp.events import PageSaved, WikiEvent
from evowiki.wikiapp.guidelines import (
    NOT_QUOTED,
    STATED_REJECTED,
    blocked_rule_texts,
    list_guidelines,
    said_rules,
)
from evowiki.wikiapp.preferences import (
    OBSERVED,
    REDACTED,
    items_from_session,
    land_preference,
    redact_session_evidence,
)
from evowiki.wikiapp.refine_apply import refine_input
from evowiki.wikiapp.refine_spec import WikiSpecSource
from evowiki.wikiapp.repo import Commit
from evowiki.wikiapp.signals import all_signals
from evowiki.wikiapp.teachspec import page_teach
from tests.assistant.test_action_tools import INFO, T0, chat_turns, turn_deps
from tests.assistant.test_chat_first_tools import setup
from tests.wikiapp.test_organize import add_page
from tests.wikiapp.test_review_flow import LATER

pytestmark = pytest.mark.usefixtures("every_skill")   # tools called without a load (stage 3)

LIKES = "这页没有组件，我喜欢组件多一点的页面"
COMPLAINT = "现在你给我的摸底问题有点重复，我都说了我不会了，你为什么还要再问一遍"
RULE = "我喜欢组件多一点的页面"           # his words as the rule (rules.drafted)
PROBLEM = "他说过不会以后，摸底又问了同一个点"
GLOBAL = MemoryScope(scope_type="global")


def prefs_env(pool):
    """An organized page, the real ledger + executors, the profile fact executor."""
    e = setup(pool)
    e.profile = PgProfileStore(pool, lambda: LATER)
    e.ledger.register("profile_fact", ProfileFactExecutor(e.profile, pool))
    return e


@pytest.fixture
def env(pool):
    return prefs_env(pool)


def ctx(*words, page_id=None, mid="m1"):
    """`words[0]` is this message (id `mid`), the rest earlier ones (m0, m-1, ...)."""
    past = tuple((f"m{-n}", w) for n, w in enumerate(words[1:]))
    return ToolContext(user_id="u1", session_id="s1", page_id=page_id, user_words=words,
                       current_words=words[:1], message_id=mid, past_messages=past)


def own_items(e):
    return [i for i in tspec.effective_spec(e.state()).items
            if i.origin not in ("default", "seed")]


def remember(e, c, **args):
    return e.tools["remember_preference"].run({"applies": "lasting", **args}, c)


# --- his words in this message: active at once ------------------------------------------------

def test_a_stated_preference_lands_at_once_with_its_provenance_and_undo_blocks_it(env):
    res = remember(env, ctx(LIKES, page_id=env.pid), quote="我喜欢组件多一点的页面", text=RULE)
    assert res.error is None, res.content
    row = env.ledger.get("u1", res.action.action_id)
    assert (row.kind, row.scope, row.level, row.status) == ("stated_rule", "teaching_params",
                                                            "auto_undo", "executed")
    assert res.action.title == f"记下了：{RULE}" and res.action.can_undo and not res.action.confirm
    [item] = own_items(env)
    # the user's own rule, his words verified in this message (contract §7 #4): never RSI's
    assert (item.text, item.origin, item.authority, item.origin_ref) == (
        RULE, "user_chat", "user", "s1/m1")
    assert env.state().item_evidence[item.item_id] == "你说过「我喜欢组件多一点的页面」"
    assert [i.text for i in said_rules(env.state())] == [RULE]
    [g] = list_guidelines(env.state()).items
    assert (g.text, g.evidence) == (RULE, "你说过「我喜欢组件多一点的页面」")

    done = env.run("undo", action_id=row.action_id)
    assert done.error is None, done.content
    assert own_items(env) == [] and env.ledger.get("u1", row.action_id).status == "undone"
    [d] = env.state().rule_decisions
    assert (d.action, d.via, d.rule_id) == ("revoked", "ledger_undo", item.item_id)
    assert blocked_rule_texts(env.state()) == (RULE,)
    # no resurrection: an inferred (no quote) or earlier-message copy of it is refused ...
    again = remember(env, ctx("好的", LIKES), quote="", text=RULE + "。")
    assert again.error is not None and STATED_REJECTED in again.content
    hist = remember(env, ctx("好的", LIKES), quote="我喜欢组件多一点的页面", text=RULE)
    assert hist.error is not None and STATED_REJECTED in hist.content
    # ... but his own restatement in this message lands, recorded `reaccepted`
    back = remember(env, ctx(LIKES, mid="m2"), quote="我喜欢组件多一点的页面", text=RULE)
    assert back.error is None, back.content
    assert env.state().rule_decisions[-1].action == "reaccepted"
    assert blocked_rule_texts(env.state()) == ()


@pytest.mark.parametrize("text", ["以后所有页面都降低难度", "讲简单点"])
def test_a_rule_reworded_away_from_his_words_waits_for_his_yes(env, text):
    """Acceptance case 2 (contract §6): 「这里太难了」 drafted as something else is not his rule
    (decided in code: the rule must say what he said)."""
    res = remember(env, ctx("这里太难了"), quote="这里太难了", text=text)
    assert res.error is None, res.content
    [item] = own_items(env)
    assert (item.origin, item.authority, item.origin_ref) == ("assistant_drafted", "pending", "s1/m1")
    assert env.ledger.get("u1", res.action.action_id).status == "proposed"


def test_confirming_an_edited_rule_saves_the_edit_and_can_undo_it(env):
    res = remember(env, ctx("这里太难了"), quote="这里太难了", text="讲简单点")
    action_id = res.action.action_id
    edited = "每章先用一个日常例子说明问题，再解释公式"
    out = env.ledger.approve("u1", action_id, draft={"text": edited})
    assert out.status == "executed"
    assert out.content["text"] == edited
    [item] = own_items(env)
    assert (item.text, item.authority) == (edited, "user")
    assert out.undo["text"] == edited
    assert not env.state().stale_marks
    assert env.ledger.approve("u1", action_id, draft={"text": "再次修改"}).content["text"] == edited
    env.ledger.undo("u1", action_id)
    assert own_items(env) == []


def test_saving_a_rule_draft_waits_for_confirmation(env):
    res = remember(env, ctx("这里太难了"), quote="这里太难了", text="讲简单点")
    action_id = res.action.action_id
    out = env.ledger.edit_draft("u1", action_id, {"text": "每章先讲一个例子"})
    assert out.status == "proposed" and out.content["text"] == "每章先讲一个例子"
    assert own_items(env)[0].text == "讲简单点"
    env.ledger.approve("u1", action_id)
    assert own_items(env)[0].text == "每章先讲一个例子"


@pytest.mark.parametrize("draft", [{"text": "   "}, {"text": 42}, {"text": "x", "replaces": "other"},
                                   {"text": "字" * (tspec.MAX_ITEM_TEXT + 1)}])
def test_invalid_rule_edits_do_not_activate_a_rule(env, draft):
    res = remember(env, ctx("这里太难了"), quote="这里太难了", text="讲简单点")
    out = env.ledger.approve("u1", res.action.action_id, draft=draft)
    assert out.status == "failed"
    [item] = own_items(env)
    assert (item.text, item.authority) == ("讲简单点", "pending")


def test_a_quote_that_is_not_his_words_or_a_repeat_is_refused_and_writes_nothing(env):
    made_up = remember(env, ctx(LIKES), quote="我喜欢交互多的页面", text=RULE)
    assert made_up.error is not None and NOT_QUOTED_ANY in made_up.content
    assert env.rows() == [] and own_items(env) == []
    assert remember(env, ctx(LIKES), quote=RULE, text=RULE).error is None
    twice = remember(env, ctx(LIKES, mid="m2"), quote=RULE, text=RULE)
    assert twice.error is None and ALREADY_IN_FORCE in twice.content
    assert len(own_items(env)) == 1 and len(env.rows()) == 1


def test_a_this_task_request_writes_nothing(env):
    res = env.tools["remember_preference"].run(
        {"applies": "this_task", "quote": "这道题讲简单点", "text": "讲简单点"}, ctx("这道题讲简单点"))
    assert res.error is None and res.content == THIS_TASK
    fact = env.tools["remember_about_me"].run(
        {"applies": "this_task", "section": "我是谁", "quote": "今天只有半小时",
         "text": "今天只有半小时"}, ctx("今天只有半小时"))
    assert fact.error is None and fact.content == THIS_TASK
    assert env.rows() == [] and own_items(env) == []


def test_a_fact_about_one_domain_names_it_in_the_profile_line(env):
    res = env.tools["remember_about_me"].run(
        {"applies": "lasting", "section": WHO, "quote": "python 我什么都不会", "text": "什么都不会",
         "topic": "python"}, ctx("python 我什么都不会"))
    assert res.error is None and "python：什么都不会" in res.content


def test_the_page_writer_reads_the_adopted_principle(env):
    remember(env, ctx(LIKES), quote=RULE, text=RULE)
    snap = env.repo.load("u1")
    inp = refine_input(snap, env.pid, LATER, WikiSpecSource(env.repo), mode="rewrite")
    assert RULE in [i.text for i in inp.teaching.items]
    assert RULE in preread_spec(inp.teaching)                 # the write prompt's spec part


# --- words from an earlier message, or inferred: pending until he confirms ---------------------

def test_an_earlier_message_quote_waits_for_his_confirmation_and_teaches_nothing_until_then(env):
    res = remember(env, ctx("好的", LIKES), quote="我喜欢组件多一点的页面", text=RULE)
    assert res.error is None, res.content
    row = env.ledger.get("u1", res.action.action_id)
    assert (row.kind, row.status, row.level) == ("rule", "proposed", "ask")
    assert res.action.confirm and res.action.status == "proposed"
    [item] = own_items(env)
    assert (item.origin, item.authority, item.origin_ref) == ("memory_backfill", "pending", "s1/m0")
    assert RULE not in page_teach(env.state(), env.pid).guidelines     # pending: not in force
    assert said_rules(env.state()) == ()
    env.ledger.approve("u1", row.action_id)                             # his 确认
    [item] = own_items(env)
    assert (item.origin, item.authority) == ("memory_backfill", "user")
    assert env.state().rule_decisions[-1].action == "reaccepted"
    assert RULE in page_teach(env.state(), env.pid).guidelines
    assert env.ledger.get("u1", row.action_id).status == "executed"


def test_an_inferred_preference_is_a_card_and_rejecting_it_blocks_it(env):
    res = remember(env, ctx(LIKES), quote="", text=RULE)
    assert res.error is None, res.content
    [item] = own_items(env)
    assert (item.origin, item.authority) == ("assistant_drafted", "pending")
    env.ledger.reject("u1", res.action.action_id)
    assert own_items(env) == []
    assert env.state().rule_decisions[-1].action == "rejected"
    assert blocked_rule_texts(env.state()) == (RULE,)


def test_removing_a_pending_rule_in_params_closes_its_card(env):
    from evowiki.wikiapp.guidelines import remove_guideline
    res = remember(env, ctx(LIKES), quote="", text=RULE)
    remove_guideline(env.repo, "u1", own_items(env)[0].item_id, now=LATER, new_id=env.ids)
    assert env.ledger.get("u1", res.action.action_id).status == "rejected"


# --- conflicts (S3-7) ----------------------------------------------------------------------------

def test_a_newer_preference_supersedes_the_old_one_keeps_its_history_and_undo_restores_it(env):
    old = remember(env, ctx("以后都先给例子再讲定义"), quote="以后都先给例子再讲定义",
                   text="以后都先给例子再讲定义")
    [first] = own_items(env)
    new = remember(env, ctx("算了，以后都先讲定义再举例", mid="m2"), quote="以后都先讲定义再举例",
                   text="以后都先讲定义再举例", replaces=first.item_id)
    assert new.error is None, new.content
    [now] = own_items(env)
    assert now.text == "以后都先讲定义再举例" and now.origin_ref == "s1/m2"
    st = env.state()
    # the old one: out of force, its decision, words and source kept
    d = st.rule_decisions[-1]
    assert (d.action, d.rule_id, d.text) == ("revoked", first.item_id, "以后都先给例子再讲定义")
    assert st.item_evidence[first.item_id] == "你说过「以后都先给例子再讲定义」"
    assert env.ledger.get("u1", new.action.action_id).result["superseded"] == [first.item_id]
    assert "以后都先给例子再讲定义" not in page_teach(st, env.pid).guidelines
    env.run("undo", action_id=new.action.action_id)
    [back] = own_items(env)
    assert (back.item_id, back.text, back.origin_ref) == (first.item_id, "以后都先给例子再讲定义",
                                                          "s1/m1")
    assert old.error is None


def test_a_negated_rule_is_not_a_duplicate_of_a_rejected_one(env):
    first = remember(env, ctx(LIKES), quote="", text="先给例子")
    env.ledger.reject("u1", first.action.action_id)
    res = remember(env, ctx(LIKES), quote="", text="不要先给例子")
    assert res.error is None, res.content


# --- the memory session's call -------------------------------------------------------------------

def _memory_land(env, kind="teaching", text=RULE, key="cand1", provenance="explicit_history"):
    ev = (EvidenceRef(message_id="m9", session_id="s0", role="user", quote="我喜欢组件多一点的页面"),)
    return land_preference(env.repo, env.repo.load("u1"), kind=kind, text=text,
                           provenance=provenance, evidence=ev, scope=GLOBAL, session_id=None,
                           idempotency_key=key, now=LATER, new_id=env.ids)


def test_memory_backfill_is_pending_and_the_same_key_finds_the_same_row(env):
    a = _memory_land(env)
    assert a.status == "pending" and a.pending_action_id is not None
    b = _memory_land(env)
    assert (b.status, b.pending_action_id, b.item_id) == ("pending", a.pending_action_id, a.item_id)
    assert len(env.rows()) == 1 and len(own_items(env)) == 1
    env.ledger.approve("u1", a.pending_action_id)
    assert _memory_land(env).status == "active"            # executed: his now
    assert _memory_land(env, provenance="observed", key="k2").reason == OBSERVED


def test_a_backfilled_fact_is_a_card_and_confirming_writes_it_with_its_provenance(env):
    got = _memory_land(env, kind="about_me", text="每天只有半小时")
    assert got.status == "pending"
    row = env.ledger.get("u1", got.pending_action_id)
    assert (row.kind, row.status) == ("profile_fact", "proposed")
    assert env.profile.get("u1").owned == {}
    env.ledger.approve("u1", row.action_id)
    p = env.profile.get("u1")
    assert p.owned[WHO] == ("每天只有半小时",)
    meta = p.fact_meta[line_key(WHO, "每天只有半小时")]
    assert (meta.provenance, meta.origin_ref, meta.action_id) == (
        "explicit_history", "s0/m9", row.action_id)


# --- 怎么学比较好: his rules only, and each rule reaches a model once -------------------------

def test_said_lists_only_his_own_rules_and_the_profile_block_leaves_them_to_the_spec(env):
    remember(env, ctx(LIKES), quote=RULE, text=RULE)
    remember(env, ctx("好的", "我要每页都有小结"), quote="每页都有小结", text="每页最后加一个小结")
    snap = env.repo.load("u1")
    lines = derived(snap, LATER)
    assert lines[HOW] == (RULE,)          # not the defaults, not RSI's, not the pending one
    block = context_for(lines, "assistant.turn")
    assert RULE not in block                                   # the principles list has it
    assert tspec.wiki_texts(snap.state).count(RULE) == 1


# --- restyle_pages (T7) ------------------------------------------------------------------------

def test_a_new_rule_restyles_nothing_and_restyle_pages_marks_exactly_the_confirmed_pages(env):
    remember(env, ctx(LIKES), quote=RULE, text=RULE)
    assert env.state().stale_marks == {}                       # landing a rule marks no page
    [item] = own_items(env)
    assert restyle_candidates(env.state(), item.item_id) == [env.pid]
    res = env.tools["restyle_pages"].run({"item_id": item.item_id}, ctx("把以前的页都改成这样"))
    assert res.error is None, res.content
    assert res.action.confirm and env.state().stale_marks == {}
    env.ledger.approve("u1", res.action.action_id)
    assert set(env.state().stale_marks) == {env.pid}
    env.ledger.undo("u1", res.action.action_id)
    assert env.state().stale_marks == {}


# --- a deleted session ---------------------------------------------------------------------------

def test_items_from_session_and_redaction_under_the_forget_gate(env, pool):
    remember(env, ctx(LIKES), quote="我喜欢组件多一点的页面", text=RULE)
    env.tools["remember_about_me"].run({"applies": "lasting", "section": WHO,
                                        "quote": "我是护士", "text": "是护士"}, ctx("我是护士", mid="m2"))
    with pool.connection() as conn:
        conn.execute("insert into assistant_session (session_id, user_id, created_at) "
                     "values ('s1', 'u1', %s) on conflict do nothing", (LATER,))
    got = items_from_session(env.state(), env.profile.get("u1"), "s1")
    assert [i.text for i in got.rules] == [RULE] and [f[1] for f in got.facts] == ["是护士"]
    assert items_from_session(env.state(), env.profile.get("u1"), "s9").rules == ()
    with pool.connection() as conn, conn.transaction():
        rid = conn.execute("select evowiki_forget_request('u1', 's1', null, 'api:test') as r"
                           ).fetchone()["r"]
        conn.execute("select evowiki_forget_open(%s)", (rid,))
        out = redact_session_evidence(conn, "u1", "s1", request_id=rid)
        conn.execute("select evowiki_forget_close(%s)", (rid,))
    assert out["wiki_event"] >= 1 and out["learner_profile"] == 1 and out["agent_action"] == 2
    st = env.repo.load("u1").state
    [item] = own_items(env)
    assert item.text == RULE and item.origin_ref == "deleted:s1/m1"   # the rule stays
    assert st.item_evidence[item.item_id] == REDACTED
    meta = env.profile.get("u1").fact_meta[line_key(WHO, "是护士")]
    assert (meta.origin_ref, meta.evidence) == ("deleted:s1/m2", REDACTED)
    with pool.connection() as conn:
        quotes = [r["q"] for r in conn.execute(
            "select content ->> 'quote' as q from agent_action where user_id = 'u1'").fetchall()]
    assert quotes == [REDACTED, REDACTED]


def test_redaction_outside_the_gate_is_refused(env, pool):
    import psycopg
    remember(env, ctx(LIKES), quote=RULE, text=RULE)
    with pool.connection() as conn, conn.transaction(), pytest.raises(psycopg.Error):
        redact_session_evidence(conn, "u1", "s1", request_id="none")


# --- report_problem --------------------------------------------------------------------------

def test_a_complaint_becomes_a_user_complaint_signal_and_a_quiet_reply(env):
    tool = env.tools["report_problem"]
    res = tool.run({"quote": "我都说了我不会了，你为什么还要再问一遍", "problem": PROBLEM},
                   ctx(COMPLAINT, page_id=env.pid))
    assert res.error is None and "已记下这个问题" in res.content and res.action is None
    [sig] = all_signals(env.repo, "u1", kinds=frozenset({"user_complaint"}))
    assert sig.page_id == env.pid
    assert sig.text == f"{PROBLEM}。原话：「我都说了我不会了，你为什么还要再问一遍」"
    bad = tool.run({"quote": "你老是重复", "problem": PROBLEM}, ctx(COMPLAINT))
    assert bad.error is not None and NOT_QUOTED in bad.content
    assert len(all_signals(env.repo, "u1", kinds=frozenset({"user_complaint"}))) == 1


# --- whole turns on the scripted chat model ---------------------------------------------------

def test_the_loop_prompt_ties_promises_to_tool_results():
    rules = library().get("remember").rules        # stage 3: moved into the remember Skill
    assert "remember_preference" in rules and "applies=this_task" in rules
    assert "report_problem" in library().get("settings").rules
    assert "restyle_pages" in library().get("settings").rules
    assert "「他可能不懂微积分」" in rules


def test_a_turn_records_the_preference_and_the_chat_shows_the_quiet_line(tmp_path, env):
    call = ToolCall(id="c1", name="remember_preference",
                    args={"applies": "lasting", "quote": RULE, "text": RULE})
    prov = chat_turns(ChatTurn(text="", tool_calls=(call,)),
                      ChatTurn(text="好，这页我先给你加一个能动手的小组件。"))
    deps = turn_deps(tmp_path, env, prov, list(env.tools.values()))
    ev = list(run_turn(deps, INFO, MessageIn(text=LIKES)))
    act = next(e for e in ev if e.type == "action")
    assert (act.kind, act.status, act.title) == ("stated_rule", "executed", f"记下了：{RULE}")
    [item] = own_items(env)
    # the evidence names the logged message of this turn
    st = fold(INFO, deps.store.entries("s1"))
    assert item.origin_ref == f"s1/{st.turns[-1].message_id}"
    tool_msg = prov.chat_calls[1].messages[-1]
    assert "已记下" in tool_msg.text and RULE in tool_msg.text
    out = session_out(st)
    assert [c.kind for c in out.actions] == ["stated_rule"]


def test_a_lasting_request_with_a_local_example_reaches_the_real_prompt(tmp_path, env):
    rule = "以后每章都先解释符号，再讲公式"
    message = rule + "，比如这一章，把这里的符号解释补上。"
    call = ToolCall(id="c1", name="remember_preference",
                    args={"applies": "lasting", "quote": rule, "text": rule})
    prov = chat_turns(ChatTurn(tool_calls=(call,)), ChatTurn(text="已记下。"))
    deps = turn_deps(tmp_path, env, prov, list(env.tools.values()))
    list(run_turn(deps, INFO, MessageIn(text=message)))
    prompt = prov.chat_calls[0].system
    assert "quote 和 text 都照抄" in prompt
    assert "记下长期要求，并载入对应技能为当前页生成修改草稿" in prompt
    assert "不代表要批量改写所有旧页面" in prompt
    [item] = own_items(env)
    assert item.text == rule and item.authority == "user"
    assert not env.state().stale_marks


def test_a_turn_reports_the_complaint_as_a_quiet_row(tmp_path, env):
    call = ToolCall(id="c1", name="report_problem",
                    args={"quote": "我都说了我不会了", "problem": PROBLEM})
    prov = chat_turns(ChatTurn(text="", tool_calls=(call,)),
                      ChatTurn(text="抱歉，这个点你已经说过不会了，我们直接从头讲。"))
    deps = turn_deps(tmp_path, env, prov, list(env.tools.values()))
    ev = list(run_turn(deps, INFO, MessageIn(text=COMPLAINT)))
    start = next(e for e in ev if e.type == "tool_start")
    end = next(e for e in ev if e.type == "tool_end")
    assert (start.name, start.effect, start.label) == ("report_problem", "read", "正在记下这个问题")
    assert (end.ok, end.label) == (True, "已记下这个问题")
    assert not any(e.type == "action" for e in ev)
    assert len(all_signals(env.repo, "u1", kinds=frozenset({"user_complaint"}))) == 1
    st = fold(INFO, deps.store.entries("s1"))
    [row] = st.messages[-1].tools
    assert (row.name, row.effect, row.label) == ("report_problem", "read", "已记下这个问题")


@pytest.mark.parametrize("text", [LIKES, COMPLAINT])
def test_the_turn_gives_the_tools_his_messages(tmp_path, env, text):
    seen = []

    class Spy:
        spec = env.tools["report_problem"].spec

        def run(self, args, ctx):
            seen.append((ctx.user_words, ctx.message_id is not None))
            return env.tools["report_problem"].run(args, ctx)
    call = ToolCall(id="c1", name="report_problem", args={"quote": text[:6], "problem": PROBLEM})
    prov = chat_turns(ChatTurn(text="", tool_calls=(call,)), ChatTurn(text="好。"))
    tools = [t for n, t in env.tools.items() if n != "report_problem"] + [Spy()]
    deps = turn_deps(tmp_path, env, prov, tools)
    list(run_turn(deps, INFO, MessageIn(text=text)))
    assert seen == [((text,), True)]


# --- a rule he stated for a domain reaches that domain's pages and later sessions ---------------
# live 2026-10-29 (memory-d-consolidate, claude/memory-live-2092eb e126b9c9): the turn called
# remember_preference(text="数学题都先解释符号，再讲怎么算", domain="数学"); the rule was active but in
# neither page's rules (both pages without a domain) nor in session 2's principles (wiki_texts kept
# only items without a domain)

MATH_SAID = "以后数学题都先解释符号，再讲怎么算。"
MATH_RULE = "数学题都先解释符号，再讲怎么算"
MATH_LINE = f"{MATH_RULE}（只用于「数学」的内容）"


def _page_with_domain(env, title: str, domain: str | None) -> str:
    pid = add_page(env.repo, env.ids, title=title)
    if domain is not None:
        snap = env.repo.load("u1")
        rec = snap.state.pages[pid].model_copy(update={"domain": domain})
        env.repo.commit(snap, Commit(wiki=[WikiEvent(event_id=env.ids("wev"), ts=LATER,
                                                     body=PageSaved(record=rec, action="updated"))]))
    return pid


def test_a_rule_stated_for_math_applies_to_math_pages_and_to_a_later_sessions_principles(
        tmp_path, env):
    res = remember(env, ctx(MATH_SAID, page_id=env.pid), quote=MATH_SAID, text=MATH_RULE,
                   domain="数学")
    assert res.error is None, res.content
    [item] = own_items(env)
    assert (item.text, item.domain, item.authority) == (MATH_RULE, "数学", "user")
    normal = _page_with_domain(env, "正态分布", None)       # no domain: the embedding found none
    math = _page_with_domain(env, "方差", "数学")
    history = _page_with_domain(env, "科举", "历史")
    state = env.state()
    for pid in (env.pid, normal, math):
        lines = page_teach(state, pid).guidelines
        assert [g for g in lines if "先解释符号" in g] == [MATH_LINE], (pid, lines)
    assert not any("先解释符号" in g for g in page_teach(state, history).guidelines)
    assert MATH_LINE not in tspec.wiki_texts(state)              # without domains: domain-agnostic

    # a new session on the page without a domain: the principles carry the rule once
    info = SessionInfo(session_id="s2", user_id="u1", page_id=normal, note_concept_id=None,
                       created_at=T0)
    prov = chat_turns(ChatTurn(text="先说符号：x 是……"))
    store2 = MemorySessionStore()
    store2.create(info)
    deps = dc_replace(turn_deps(tmp_path, env, prov, list(env.tools.values())),
                      data=WikiData(env.repo, lambda: LATER), store=store2)
    list(run_turn(deps, info, MessageIn(text="z = (x − μ) / σ 这个公式具体怎么用？举个例子。")))
    [req] = prov.chat_calls
    text = "\n".join(m.text for m in req.messages if m.origin != "memory")
    principles = text.split("为他组织内容的原则", 1)[1]
    assert principles.count("先解释符号") == 1 and MATH_LINE in principles, principles
