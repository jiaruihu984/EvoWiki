"""Chat-first blocks, plan B2: the assistant's `new_goal` proposes a blocks draft (the blocks calls
run inside the tool, on the learner's gateway), `edit_goal_blocks` replaces it with an edited one
(the old row rejected "replaced"), and the `goal_new` executor makes the goal map from the blocks
(also from the card's edited list, `revise`). Real Postgres, the real ledger + executors, a
SCRIPTED LLM - nothing here calls a paid API."""

from dataclasses import replace

import pytest

from evowiki.agent import durable
from evowiki.agent.tools import ToolContext
from evowiki.assistant.tools.actions import (
    ASK_USE,
    BLOCKS_CONFIRMED,
    MATERIALS_UNREADABLE,
    MODEL_UNAVAILABLE,
    NO_BLOCKS_DRAFT,
    REPLACED,
    NewGoalArgs,
    build_action_tools,
)
from evowiki.bot.bot_schemas import ActionOut
from evowiki.goals.intake import OUTLINE_PURPOSE
from evowiki.sources.reader import MemorySourceReader
from evowiki.store import bot as store
from evowiki.wikiapp.llm import CountingGateway
from evowiki.wikiapp.repo import Commit, PgRepo
from tests.assistant.fakes import ungrounded
from tests.assistant.test_action_tools import ALL_ASK, CTX, Env
from tests.runtime.helpers import make_user
from tests.sources.test_store import _sec, _src
from tests.wikiapp.goal_script import auto_probe
from tests.wikiapp.script import Script
from tests.wikiapp.test_path_edits import NOW, Ids, seed_events

pytestmark = pytest.mark.usefixtures("every_skill")   # tools called without a load (stage 3)

GOAL = "我想养黑壳虾"
BLOCKS = {"title": "黑壳虾", "framing": "先把它认清楚，再把它养活。",
          "caveat": "「黑壳虾」是俗称，常和别的米虾混着卖。", "reference": "无", "blocks": [
    {"name": "认识黑壳虾", "level": "基础", "summary": "它是什么虾", "covers": ["外形", "习性"]},
    {"name": "把水养好", "level": "基础", "summary": "水的要求；水出问题时",
     "covers": ["pH", "换水", "水质检测"]},
    {"name": "日常喂养", "level": "核心", "summary": "喂什么", "covers": ["饲料"],
     "target": True}],
    "mainline": "主线是 认识黑壳虾 → 把水养好 → 日常喂养。", "start": ["pH", "换水", "饲料"],
    "question": "你是想自己养，还是当成一种生物来了解？"}


@pytest.fixture
def env(pool):
    make_user(pool, "u1")
    ids, repo = Ids(), PgRepo(pool)
    aspect, wiki = seed_events(ids)
    repo.commit(repo.load("u1"), Commit(aspect=aspect, wiki=wiki))
    with pool.connection() as conn:
        store.put_settings(conn, "u1", ALL_ASK, NOW)
    s = Script()
    auto_probe(s)
    return Env(pool, repo, ids, {"s": s}, NOW)


REC = {"definition": "", "diagnose": True, "said": [], "said_level": ""}     # a block's part of the goal record


def script(env, blocks=BLOCKS):
    env.scripts["s"].push(OUTLINE_PURPOSE, blocks)


def propose(env, **args):
    script(env)
    res = env.run("new_goal", goal=GOAL, **args)
    assert res.error is None, res.content
    return res, env.ledger.get("u1", res.action.action_id)


# --- new_goal: the blocks draft --------------------------------------------------------------

def test_new_goal_proposes_the_blocks_as_a_draft_and_writes_nothing_else(env):
    before = env.state()
    res, row = propose(env)
    s = env.scripts["s"]
    assert s.calls(OUTLINE_PURPOSE) == 1                     # one call (D08: no review call)
    assert (row.kind, row.status, row.level) == ("goal_new", "proposed", "ask")
    # the exact shape the plugin reads
    assert row.content == {
        "goal": GOAL, "words": [GOAL], "limits": [], "session_id": "s1", "said": GOAL,
        "title": "黑壳虾",
        "framing": "先把它认清楚，再把它养活。", "caveat": "「黑壳虾」是俗称，常和别的米虾混着卖。",
        "mainline": "主线是 认识黑壳虾 → 把水养好 → 日常喂养。", "start": ["pH", "换水", "饲料"],
        "question": "你是想自己养，还是当成一种生物来了解？",
        "blocks": [
            {"key": "b1", "name": "认识黑壳虾", "covers": ["外形", "习性"], "summary": "它是什么虾",
             "target": False, "existing_id": "", "level": "基础", **REC, "atomic": False,
             "sections": [], "exercises": []},
            {"key": "b2", "name": "把水养好", "covers": ["pH", "换水", "水质检测"],
             "summary": "水的要求；水出问题时", "target": False, "existing_id": "",
             "level": "基础", **REC, "atomic": False,
             "sections": [], "exercises": []},
            {"key": "b3", "name": "日常喂养", "covers": ["饲料"], "summary": "喂什么",
             "target": True, "existing_id": "", "level": "核心", **REC, "atomic": False,
             "sections": [], "exercises": []}],
        "pages": [{"key": "b1", "title": "认识黑壳虾", "covers": "外形、习性", "level": "基础"},
                  {"key": "b2", "title": "把水养好", "covers": "pH、换水、水质检测", "level": "基础"},
                  {"key": "b3", "title": "日常喂养", "covers": "饲料", "level": "核心"}],
        "confirm": True}
    assert row.summary == "「黑壳虾」分成 3 块"
    # the ? / 参数 why: where the blocks come from, from the row's data
    assert row.why == "按你说的「我想养黑壳虾」分成了 3 块"
    assert res.action.title == "「黑壳虾」分成 3 块" and res.action.confirm
    assert [(i.key, i.title, i.covers, i.level) for i in res.action.items] == [
        ("b1", "认识黑壳虾", "外形、习性", "基础"), ("b2", "把水养好", "pH、换水、水质检测", "基础"),
        ("b3", "日常喂养", "饲料", "核心")]
    assert "草稿" in res.content
    # the turn's words before the card: the blocks call's framing + caveat
    assert res.lead == "先把它认清楚，再把它养活。\n\n「黑壳虾」是俗称，常和别的米虾混着卖。"
    # what the model words the tail from: the blocks with their levels, the lead already said,
    # the three lines after the card
    for s in ("分块（卡片上已经列出来了，回复里不要再列一遍）：",
              "1. 认识黑壳虾〔基础〕：它是什么虾（包含：外形、习性）",
              "2. 把水养好〔基础〕：水的要求；水出问题时（包含：pH、换水、水质检测）",
              f"卡片前已经替你对他说了：「{res.lead}」",
              "- 主线：主线是 认识黑壳虾 → 把水养好 → 日常喂养。", "- 从哪里开始：第一块「认识黑壳虾」（外形、习性）",
              "- 问题：你是想自己养，还是当成一种生物来了解？",
              "不说卡片在上面还是下面"):
        assert s in res.content, s
    assert "自查改过分块" not in res.content           # no review: the lines name the card's blocks
    # the same card the plugin reloads from the row
    from evowiki.assistant.tools.actions import items_of
    assert [i.level for i in items_of(row.content)] == ["基础", "基础", "核心"]
    after = env.state()                          # nothing in the wiki before the confirm
    assert after.maps == before.maps and after.drafts == before.drafts


def test_new_goal_without_a_model_is_refused_and_writes_nothing(env):
    tools = {t.spec.name: t for t in ungrounded(build_action_tools(replace(env.deps, gateway=lambda u: None)))}
    res = tools["new_goal"].run({"goal": GOAL}, CTX)
    assert res.error is not None and MODEL_UNAVAILABLE in res.content
    assert env.rows() == []


def test_a_blocks_call_with_no_usable_block_is_refused(env):
    env.scripts["s"].push(OUTLINE_PURPOSE, {"title": "x", "blocks": []})
    res = env.run("new_goal", goal=GOAL)
    assert res.error is not None and "请切换模型重试" in res.content
    assert env.rows() == []


def test_a_replayed_step_finds_its_row_and_makes_no_second_call(env):
    """M4: the replayed step gives the model the same tool result as the first run (the brief is
    rebuilt from the stored row: blocks, framing, caveat, mainline, start, question)."""
    script(env)
    with durable.virtual_workflow("wf-g"):
        first = durable.step("t", lambda: env.run("new_goal", goal=GOAL).model_dump(mode="json"))
    with durable.virtual_workflow("wf-g"):
        again = durable.step("t", lambda: env.run("new_goal", goal=GOAL).model_dump(mode="json"))
    assert again["action"]["action_id"] == first["action"]["action_id"]
    assert len(env.rows()) == 1 and env.scripts["s"].calls(OUTLINE_PURPOSE) == 1
    assert "- 主线：主线是 认识黑壳虾 → 把水养好 → 日常喂养。" in first["content"]
    assert again["content"] == first["content"]
    assert again["lead"] == first["lead"] == BLOCKS["framing"] + "\n\n" + BLOCKS["caveat"]


def test_a_replayed_chat_edit_gives_the_same_changes(env):
    """M4: edit_goal_blocks stores what it changed (`changes`, `refused`) on the new row; a
    replay rebuilds the same result."""
    propose(env)
    ops = [{"op": "drop", "name": "认识黑壳虾"}, {"op": "drop", "name": "繁殖"}]
    with durable.virtual_workflow("wf-e"):
        first = durable.step("t", lambda: env.run("edit_goal_blocks", ops=ops).model_dump(
            mode="json"))
    with durable.virtual_workflow("wf-e"):
        again = durable.step("t", lambda: env.run("edit_goal_blocks", ops=ops).model_dump(
            mode="json"))
    row = env.ledger.get("u1", first["action"]["action_id"])
    assert row.content["changes"] == ["去掉了「认识黑壳虾」（它原来包含：外形、习性）"]
    assert row.content["refused"] == ["去掉「繁殖」（没有叫这个名字的块）"]
    assert "- 去掉了「认识黑壳虾」" in first["content"]
    assert again["content"] == first["content"]


class _PricedGateway(CountingGateway):
    """A CountingGateway whose every call costs 0.25 (the scripted calls are free)."""

    def _spent(self, out):
        self.cost += 0.25
        return out


def test_the_blocks_calls_are_charged_to_the_turn_and_the_card(env):
    """M3: the tool's own gateway (the blocks call) is added to the turn's spend, so the row's
    turn cost (the card's cost_usd) includes them."""
    from evowiki.bot.actions import TURN_COST
    charged: list = []
    ctx = CTX.model_copy(update={"spent_usd": lambda: 0.1 + sum(charged),
                                 "charge": charged.append})
    deps = replace(env.deps, gateway=lambda u: _PricedGateway(env.scripts["s"].gateway()))
    tools = {t.spec.name: t for t in ungrounded(build_action_tools(deps))}
    script(env)
    res = tools["new_goal"].run({"goal": GOAL}, ctx)
    assert res.error is None, res.content
    assert charged == [0.25]                                   # one call
    row = env.ledger.get("u1", res.action.action_id)
    assert row.result[TURN_COST] == pytest.approx(0.35)
    assert ActionOut.of(row).cost_usd == pytest.approx(0.35)
    # a failed blocks call still costs what it cost
    env.scripts["s"].push(OUTLINE_PURPOSE, {"title": "x", "blocks": []})
    assert tools["new_goal"].run({"goal": "我想学做面包"}, ctx).error is not None
    assert charged == [0.25, 0.25]


def test_only_the_replaced_draft_is_named_on_the_new_card(env):
    """M5: the chat edit's card says which row it replaced (content and card `replaces`); a
    second new_goal names none, so the client leaves other waiting goal cards alone."""
    _, old = propose(env)
    res = env.run("edit_goal_blocks", ops=[{"op": "drop", "name": "认识黑壳虾"}])
    assert res.action.replaces == old.action_id
    assert env.ledger.get("u1", res.action.action_id).content["replaces"] == old.action_id
    script(env)
    other = env.run("new_goal", goal="我想学做面包")
    assert other.error is None and other.action.replaces == ""
    assert env.ledger.get("u1", res.action.action_id).status == "proposed"   # untouched


def test_card_edits_are_kept_on_the_waiting_row_and_a_chat_edit_starts_from_them(env):
    """H3: each card edit (move / × / 加一块) is stored on the proposed row without approving
    (`ActionLedger.edit_draft`); edit_goal_blocks then starts from the stored blocks."""
    from evowiki.bot.actions import ActionRefused, NotDecidable
    _, row = propose(env)
    kept = env.ledger.edit_draft("u1", row.action_id, {"pages": [
        {"key": "b3"}, {"key": "b1"}, {"title": "造景"}]})
    assert kept.status == "proposed" and kept.content_hash != row.content_hash
    assert [b["name"] for b in kept.content["blocks"]] == ["日常喂养", "认识黑壳虾", "造景"]
    res = env.run("edit_goal_blocks", ops=[{"op": "rename", "name": "造景", "new_name": "水草造景"}])
    assert res.error is None, res.content
    new = env.ledger.get("u1", res.action.action_id)
    assert [b["name"] for b in new.content["blocks"]] == ["日常喂养", "认识黑壳虾", "水草造景"]
    # 加一块 with a name the draft has: refused at the edit, nothing stored
    with pytest.raises(ActionRefused, match="已经有这一块了"):
        env.ledger.edit_draft("u1", new.action_id, {"pages": [
            {"key": "b1"}, {"key": "b2"}, {"key": "b3"}, {"title": "日常 喂养"}]})
    assert env.ledger.get("u1", new.action_id).content == new.content
    # only a waiting row
    with pytest.raises(NotDecidable):
        env.ledger.edit_draft("u1", row.action_id, {"pages": [{"key": "b1"}]})   # replaced
    with pytest.raises(LookupError):
        env.ledger.edit_draft("u2", new.action_id, {"pages": [{"key": "b1"}]})


def test_a_card_rename_keeps_the_rows_key_level_covers_and_said(env):
    """{key, title} on the waiting goal card renames that block: its key, level, covers and said
    stay; an empty name is refused at the edit."""
    from evowiki.bot.actions import ActionRefused
    _, row = propose(env)
    before = {b["key"]: b for b in row.content["blocks"]}
    kept = env.ledger.edit_draft("u1", row.action_id, {"pages": [
        {"key": "b1", "title": "黑壳虾是什么"}, {"key": "b2"}, {"key": "b3"}]})
    after = {b["key"]: b for b in kept.content["blocks"]}
    assert after["b1"]["name"] == "黑壳虾是什么"
    for f in ("key", "level", "covers", "definition", "said", "said_level", "diagnose"):
        assert after["b1"][f] == before["b1"][f], f
    assert after["b2"] == before["b2"] and after["b3"] == before["b3"]
    assert kept.content["pages"][0]["title"] == "黑壳虾是什么"
    with pytest.raises(ActionRefused):
        env.ledger.edit_draft("u1", row.action_id, {"pages": [{"key": "b1", "title": " "}]})


def test_confirming_the_same_goal_while_it_is_being_learned_fails_the_row(env):
    """H4 (b) through the executor: the row fails with the refusal text."""
    from evowiki.wikiapp.goal import GOAL_IN_PROGRESS, enter_region, stop_draft
    from evowiki.wikiapp.llm import CountingGateway
    _, row = propose(env)
    done = env.ledger.approve("u1", row.action_id)
    assert done.status == "executed", done.detail
    st = env.state()
    draft_id = done.result["draft_id"]
    gw = CountingGateway(env.scripts["s"].gateway())
    if draft_id is not None:
        stop_draft(env.repo, gw, "u1", draft_id, now=NOW, new_id=env.ids)
    st = env.state()
    map_id = done.result["map_id"] or st.drafts[draft_id].map_id
    first = next(n.id for n in st.maps[map_id].nodes if n.parent is None)
    env.scripts["s"].push(OUTLINE_PURPOSE, {"blocks": [{"name": "外形", "page": True},
                                                       {"name": "习性", "page": True}]})
    enter_region(env.repo, gw, "u1", map_id, first, now=NOW, new_id=env.ids)
    _, again = propose(env, again=True)
    failed = env.ledger.approve("u1", again.action_id)
    assert (failed.status, failed.detail) == ("failed", GOAL_IN_PROGRESS)



def _reader():
    lesson = _src("s1", status="ready", title="黑壳虾饲养讲义")
    paper = _src("s2", sha="bb", status="ready", title="A shrimp paper")
    return MemorySourceReader({"u1": [(lesson, [_sec("x1", 0), _sec("x2", 1)]),
                                      (paper, [_sec("y1", 0, source="s2")])]})


def test_new_goal_from_a_material_to_learn_plans_blocks_from_its_table_of_contents(env):
    tools = {t.spec.name: t for t in build_action_tools(replace(env.deps, sources=_reader()))}
    script(env)
    res = tools["new_goal"].run({"source_ids": ["s1"]}, CTX)
    assert res.error is None, res.content
    row = env.ledger.get("u1", res.action.action_id)
    assert row.content["source_ids"] == ["s1"] and row.content["goal"] == "按《黑壳虾饲养讲义》学"
    assert len(row.content["blocks"]) == 3
    assert row.why == "按你发的《黑壳虾饲养讲义》分成了 3 块"      # from the data, not the review
    prompt = env.scripts["s"].requests(OUTLINE_PURPOSE)[0].prompt
    assert "黑壳虾饲养讲义" in prompt and "节1" in prompt          # the material's outline
    reading = MemorySourceReader({"u1": [(_src("s3", status="reading"), [])]})
    late = {t.spec.name: t for t in ungrounded(build_action_tools(replace(env.deps, sources=reading)))}
    out = late["new_goal"].run({"source_ids": ["s3"]}, CTX)
    assert out.error is not None and "资料还在读" in out.content and len(env.rows()) == 1


def test_materials_without_a_reader_are_refused_with_no_call_and_no_row(env):
    """The blocks (and 读懂这篇's units, reading-mode spec §3.1) are planned from the materials'
    sections in this turn: no reader, no plan — never a plain row left to the executor (spec
    2026-10-28 §2.8: no fallback to the old route)."""
    for args in ({"goal": "帮我读懂这篇", "source_ids": ["s2"]},
                 {"goal": "注意力", "source_ids": ["s2"], "use": "understand"},
                 {"goal": "学黑壳虾", "source_ids": ["s2"], "use": "follow"}):
        res = env.run("new_goal", **args)
        assert res.error is not None and MATERIALS_UNREADABLE in res.content
    assert env.scripts["s"].calls() == 0 and env.rows() == []


# --- how the goal uses its materials (stage 3, spec 2026-10-28 §2.5) --------------------------

def _paper_reader():
    titles = ("摘要", "1 引言", "2 方法", "参考文献")
    paper = _src("s2", sha="bb", status="ready", title="A shrimp paper", pages=8)
    return MemorySourceReader({"u1": [
        (_src("s1", status="ready", title="黑壳虾饲养讲义"), [_sec("x1", 0), _sec("x2", 1)]),
        (paper, [replace(_sec(f"y{i}", i, source="s2"), title=t)
                 for i, t in enumerate(titles)])]})


def _tools(env, reader):
    return {t.spec.name: t for t in build_action_tools(replace(env.deps, sources=reader))}


def test_a_stated_reference_plans_the_blocks_by_the_goal_and_says_no_default(env):
    script(env)
    res = _tools(env, _reader())["new_goal"].run(
        {"goal": "学养虾，这本书作为参考", "source_ids": ["s1"]}, CTX)
    assert res.error is None, res.content
    row = env.ledger.get("u1", res.action.action_id)
    assert row.content["use"] == "reference" and "use_default" not in row.content
    p = env.scripts["s"].requests(OUTLINE_PURPOSE)[0].prompt
    assert "只作参考" in p and "先后顺序跟资料走" not in p
    assert "先当参考" not in res.content


def test_a_default_use_is_said_in_the_reply_so_he_can_change_it(env):
    script(env)
    res = _tools(env, _reader())["new_goal"].run({"goal": GOAL, "source_ids": ["s1"]}, CTX)
    row = env.ledger.get("u1", res.action.action_id)
    assert (row.content["use"], row.content["use_default"]) == ("reference", True)
    assert "这本书我先当参考，你想按它的顺序学可以说" in res.content
    script(env)
    res = _tools(env, _reader())["new_goal"].run(
        {"goal": GOAL, "source_ids": ["s1"], "use": "follow"}, CTX)       # he said: follow it
    row = env.ledger.get("u1", res.action.action_id)
    assert row.content["use"] == "follow" and "use_default" not in row.content
    assert "先后顺序跟资料走" in env.scripts["s"].requests(OUTLINE_PURPOSE)[-1].prompt


def test_one_paper_and_no_words_asks_first_with_no_call_and_no_row(env):
    res = _tools(env, _paper_reader())["new_goal"].run({"source_ids": ["s2"]}, CTX)
    assert res.error is not None and ASK_USE in res.content
    assert env.scripts["s"].calls() == 0 and env.rows() == []
    script(env)
    out = _tools(env, _paper_reader())["new_goal"].run(
        {"source_ids": ["s2"], "use": "follow"}, CTX)          # his answer (understand: test_reading_entry)
    assert out.error is None and env.ledger.get(
        "u1", out.action.action_id).content["use"] == "follow"


def test_block_sections_survive_the_draft_and_a_chat_merge(env):
    script(env, {**BLOCKS, "blocks": [{**b, "sections": ["§1"]} if i == 0 else b
                                      for i, b in enumerate(BLOCKS["blocks"])]})
    res = env.run("new_goal", goal=GOAL)
    row = env.ledger.get("u1", res.action.action_id)
    assert row.content["blocks"][0]["sections"] == ["§1"]
    merged = env.run("edit_goal_blocks", ops=[{"op": "merge", "names": ["认识黑壳虾", "把水养好"]}])
    assert merged.error is None, merged.content
    edited = env.ledger.get("u1", merged.action.action_id)
    assert edited.content["blocks"][0]["sections"] == ["§1"]   # a merge keeps every part's


# --- edit_goal_blocks -------------------------------------------------------------------------

def test_a_chat_edit_writes_a_new_draft_and_the_old_one_is_replaced(env):
    _, old = propose(env)
    res = env.run("edit_goal_blocks", ops=[
        {"op": "drop", "name": "认识黑壳虾"},
        {"op": "rename", "name": "日常喂养", "new_name": "喂养"},
        {"op": "add", "name": "造景", "covers": ["水草"], "position": 1},
        {"op": "move", "cover": "水质检测", "from_block": "把水养好", "to_block": "喂养"},
        {"op": "place", "name": "喂养", "position": 2}])
    assert res.error is None, res.content
    new = env.ledger.get("u1", res.action.action_id)
    gone = env.ledger.get("u1", old.action_id)
    assert (gone.status, gone.detail) == ("rejected", REPLACED)
    assert new.status == "proposed" and new.summary == "「黑壳虾」分成 3 块"
    assert {k: new.content[k] for k in ("goal", "words", "limits", "session_id", "title")} == {
        "goal": GOAL, "words": [GOAL], "limits": [], "session_id": "s1", "title": "黑壳虾"}
    assert [(b["key"], b["name"], b["covers"], b["level"]) for b in new.content["blocks"]] == [
        ("b1", "造景", ["水草"], ""), ("b2", "喂养", ["饲料", "水质检测"], "核心"),
        ("b3", "把水养好", ["pH", "换水"], "基础")]          # edited rows keep their levels
    assert new.content["pages"][1] == {"key": "b2", "title": "喂养", "covers": "饲料、水质检测",
                                       "level": "核心"}
    # framing / caveat carried over; the lines that named the old blocks are cleared
    assert (new.content["framing"], new.content["caveat"]) == (old.content["framing"],
                                                               old.content["caveat"])
    assert (new.content["mainline"], new.content["start"], new.content["question"]) == ("", [], "")
    # the result says what changed, op by op, and the new blocks
    for s in ("改了什么：", "- 去掉了「认识黑壳虾」（它原来包含：外形、习性）",
              "- 「日常喂养」改名为「喂养」", "- 加了「造景」，在第 1 块",
              "- 「水质检测」从「把水养好」挪到了「喂养」", "- 「喂养」挪到了第 2 块",
              "现在的分块（卡片上已经列出来了，回复里不要再列一遍）：", "2. 喂养〔核心〕"):
        assert s in res.content, s
    assert [r.status for r in env.rows()].count("proposed") == 1


def test_ops_that_cannot_be_done_are_said_and_all_of_them_refuse(env):
    propose(env)
    part = env.run("edit_goal_blocks", ops=[{"op": "drop", "name": "繁殖"},
                                            {"op": "merge", "names": ["认识黑壳虾", "把水养好"]}])
    assert part.error is None and "去掉「繁殖」（没有叫这个名字的块）" in part.content
    assert [i.title for i in part.action.items] == ["认识黑壳虾", "日常喂养"]
    n = len(env.rows())
    none = env.run("edit_goal_blocks", ops=[{"op": "rename", "name": "认识黑壳虾",
                                             "new_name": "日常喂养"}])
    assert none.error is not None and "已经有一块叫这个名字" in none.content
    assert len(env.rows()) == n                               # nothing written


def test_no_draft_and_a_confirmed_draft_are_refused(env):
    assert NO_BLOCKS_DRAFT in env.run("edit_goal_blocks", ops=[{"op": "drop", "name": "x"}]).content
    _, row = propose(env)
    other = ToolContext(user_id="u1", session_id="s2")          # another session's draft: none
    tool = env.tools["edit_goal_blocks"]
    assert NO_BLOCKS_DRAFT in tool.run({"ops": [{"op": "drop", "name": "水质"}]}, other).content
    assert env.ledger.approve("u1", row.action_id).status == "executed"
    out = env.run("edit_goal_blocks", ops=[{"op": "drop", "name": "日常喂养"}])
    assert out.error is not None and out.content.endswith(BLOCKS_CONFIRMED)


# --- the executor -------------------------------------------------------------------------------

def test_confirming_the_blocks_makes_the_map_from_them_with_no_intake_call(env):
    _, row = propose(env)
    calls = env.scripts["s"].calls(OUTLINE_PURPOSE)
    done = env.ledger.approve("u1", row.action_id)
    assert done.status == "executed", done.detail
    assert env.scripts["s"].calls(OUTLINE_PURPOSE) == calls     # no intake call on confirm
    assert {"draft_id", "map_id", "open", "calls"} <= set(done.result)
    # the card the plugin gets carries the ids 学点新的 continues with, nothing else of the result
    out = ActionOut.of(done)
    assert out.result == {k: done.result[k] for k in ("draft_id", "map_id", "path_id", "page_id", "open")}
    m = env.state().maps[done.result["map_id"] or env.state().drafts[
        done.result["draft_id"]].map_id]
    assert m.title == "黑壳虾"
    assert [(n.name, n.parent, n.members, n.scope) for n in m.nodes] == [
        ("认识黑壳虾", None, (), "外形、习性"), ("把水养好", None, (), "pH、换水、水质检测"),
        ("日常喂养", None, (), "饲料")]
    undone = env.run("undo", action_id=row.action_id)
    assert undone.error is None and undone.action.status == "undone"


def test_the_cards_edited_list_becomes_the_blocks(env):
    _, row = propose(env)
    done = env.ledger.approve("u1", row.action_id, draft={"pages": [
        {"key": "b3"}, {"key": "b1", "covers": "外形"}, {"title": "造景"}]})
    assert done.status == "executed", done.detail
    assert [(b["name"], b["covers"]) for b in done.content["blocks"]] == [
        ("日常喂养", ["饲料"]), ("认识黑壳虾", ["外形"]), ("造景", [])]
    st = env.state()
    m = st.maps[done.result["map_id"] or st.drafts[done.result["draft_id"]].map_id]
    assert [(n.name, n.scope) for n in m.nodes] == [("日常喂养", "饲料"), ("认识黑壳虾", "外形"),
                                                    ("造景", "")]


# --- intake@v27 through the tools: notes, levels, the map ------------------------------------------

def test_new_goal_passes_the_web_notes_to_the_blocks_call_fenced(env):
    from evowiki.goals.intake import DATA_BEGIN, DATA_NOTE
    _, row = propose(env, notes="「黑壳虾」是俗称，一般指新米虾属的一种，常混着卖。")
    prompt = env.scripts["s"].requests(OUTLINE_PURPOSE)[0].prompt
    assert "助手刚在网上查到的、关于这个目标的要点" in prompt and DATA_NOTE in prompt
    assert prompt.index(DATA_BEGIN) < prompt.index("一般指新米虾属的一种")
    assert "notes" not in row.content                        # the notes are the call's, not the row's
    assert len(NewGoalArgs.model_json_schema()["properties"]["notes"]["description"]) > 0
    with pytest.raises(ValueError):                            # schema-typed, bounded
        NewGoalArgs(goal=GOAL, notes="很长" * 800)


def test_a_bad_level_from_the_blocks_call_is_refused_and_writes_nothing(env):
    bad = {**BLOCKS, "blocks": [{**BLOCKS["blocks"][0], "level": "很重要"}]}
    s = env.scripts["s"]
    for _ in range(3):                                         # the gateway's parse retries too
        s.push(OUTLINE_PURPOSE, bad)
    res = env.run("new_goal", goal=GOAL)
    assert res.error is not None and "请切换模型重试" in res.content
    assert env.rows() == []


def test_chat_edits_set_levels_and_say_where_merged_contents_went(env):
    propose(env)
    res = env.run("edit_goal_blocks", ops=[
        {"op": "merge", "names": ["认识黑壳虾", "把水养好"], "name": "认识与水质"},
        {"op": "level", "name": "日常喂养", "level": "基础"},
        {"op": "add", "name": "繁殖", "covers": ["抱卵"], "level": "进阶"},
        {"op": "level", "name": "繁殖", "level": ""}])            # no level word: cannot be done
    assert res.error is None, res.content
    row = env.ledger.get("u1", res.action.action_id)
    assert [(b["name"], b["level"]) for b in row.content["blocks"]] == [
        ("认识与水质", "基础"), ("日常喂养", "基础"), ("繁殖", "进阶")]
    assert row.why == "按你说的「我想养黑壳虾」分成了 3 块"     # the count follows the edit
    assert [i.level for i in res.action.items] == ["基础", "基础", "进阶"]
    for s in (("- 「认识黑壳虾」「把水养好」合并成了「认识与水质」，原来的外形、习性、pH、换水、水质检测"
               "现在都在「认识与水质」里"), "- 「日常喂养」的分级改成了「基础」",
              "- 加了「繁殖」〔进阶〕，在第 3 块",
              "其中这些没做成，要照实告诉用户：改「繁殖」的分级（分级只能是基础、核心、进阶、拓展）"):
        assert s in res.content, s


def test_the_map_keeps_the_confirmed_levels_and_the_card_can_change_one(env):
    from evowiki.wikiapp.maps import map_out
    _, row = propose(env)
    done = env.ledger.approve("u1", row.action_id, draft={"pages": [
        {"key": "b3", "level": "进阶"}, {"key": "b1"}, {"title": "造景"},
        {"title": "繁殖", "level": "拓展"}]})
    assert done.status == "executed", done.detail
    assert [(p["title"], p["level"]) for p in done.content["pages"]] == [
        ("日常喂养", "进阶"), ("认识黑壳虾", "基础"), ("造景", ""), ("繁殖", "拓展")]
    snap = env.repo.load("u1")
    map_id = done.result["map_id"] or snap.state.drafts[done.result["draft_id"]].map_id
    m = snap.state.maps[map_id]
    assert [(n.name, n.level) for n in m.nodes] == [("日常喂养", "进阶"), ("认识黑壳虾", "基础"),
                                                    ("造景", ""), ("繁殖", "拓展")]
    out = map_out(snap.state, snap.k, map_id, NOW)
    assert [(n.name, n.level) for n in out.nodes if n.parent is None] == [
        ("日常喂养", "进阶"), ("认识黑壳虾", "基础"), ("造景", ""), ("繁殖", "拓展")]


def test_a_card_level_outside_the_four_words_is_refused(env):
    _, row = propose(env)
    done = env.ledger.approve("u1", row.action_id, draft={"pages": [{"key": "b1", "level": "必学"}]})
    assert done.status != "executed" and "分级只能是" in (done.detail or "")


LEAD = BLOCKS["framing"] + "\n\n" + BLOCKS["caveat"]
TAIL = "主线是 认识黑壳虾 → 把水养好 → 日常喂养。你是想自己养，还是当成生物来了解？"


def first_turn(tmp_path, env, *, durable_deps=False):
    """「我想养黑壳虾」: the model calls new_goal without writing, then writes the tail."""
    from evowiki.llm.chat import ChatTurn, ToolCall
    from tests.assistant.test_action_tools import chat_turns, turn_deps
    call = ToolCall(id="g1", name="new_goal", args={
        "goal": GOAL, "notes": "「黑壳虾」是俗称。"})
    prov = chat_turns(ChatTurn(tool_calls=(call,)), ChatTurn(text=TAIL))
    deps = turn_deps(tmp_path, env, prov, list(env.tools.values()))
    if durable_deps:
        from evowiki.assistant.durable import durable_turn_deps
        deps = durable_turn_deps(deps)
    return prov, deps


def test_a_first_turn_leads_with_the_blocks_framing_then_the_card_then_the_tail(tmp_path, env):
    """assistant@v23: the model writes nothing before new_goal; the turn says the blocks call's
    framing + caveat (the tool result's `lead`) → the action card → the model's tail (main line,
    where to start, the question), worded from the tool result, which says the lead was said."""
    from evowiki.assistant.contracts import MessageIn
    from evowiki.assistant.loop import run_turn
    from tests.assistant.test_action_tools import INFO
    script(env)
    prov, deps = first_turn(tmp_path, env)
    ev = [e for e in run_turn(deps, INFO, MessageIn(text="我想养黑壳虾"))
          if e.type in ("text_delta", "action")]
    assert [e.type for e in ev] == ["text_delta", "action", "text_delta"]
    assert ev[0].text == LEAD and ev[2].text == "\n\n" + TAIL
    assert [i.level for i in ev[1].items] == ["基础", "基础", "核心"]
    told = next(m for m in prov.chat_calls[1].messages if m.role == "tool").text
    assert "1. 认识黑壳虾〔基础〕" in told and "- 问题：你是想自己养" in told
    assert f"卡片前已经替你对他说了：「{LEAD}」" in told and "- 开头" not in told
    assert "「黑壳虾」是俗称。" in env.scripts["s"].requests(OUTLINE_PURPOSE)[0].prompt
    # a reload keeps the card's place: text[:card_at] is the lead, said once
    from evowiki.assistant.session import fold, session_out
    msg = fold(INFO, deps.store.entries("s1")).messages[-1]
    assert msg.card_at == len(LEAD) and msg.text == LEAD + "\n\n" + TAIL
    out = session_out(fold(INFO, deps.store.entries("s1")))
    assert out.messages[-1].card_at == msg.card_at


def test_a_replayed_first_turn_leads_once_with_the_same_card_at(tmp_path, env):
    """The lead comes from the tool result alone (rebuilt from the stored row when the step is
    replayed, `_ActionTool._run`'s found row): the same turn run again in the same workflow —
    its new_goal step finds the row, no second blocks call — hands out the same events, the lead
    once before the card, and logs the same answer and card_at."""
    from evowiki.agent import durable
    from evowiki.assistant.contracts import MessageIn
    from evowiki.assistant.loop import run_turn
    from evowiki.assistant.session import fold
    from tests.assistant.test_action_tools import INFO
    script(env)
    runs, answers = [], []
    for i in range(2):
        _, deps = first_turn(tmp_path / str(i), env, durable_deps=True)
        with durable.virtual_workflow("turn:s1:m1"):
            runs.append([e.model_dump(mode="json")
                         for e in run_turn(deps, INFO, MessageIn(text="我想养黑壳虾"),
                                           message_id="m1")
                         if e.type in ("text_delta", "action")])
        msg = fold(INFO, deps.store.entries("s1")).messages[-1]
        answers.append((msg.text, msg.card_at))
    assert runs[0] == runs[1] and answers[0] == answers[1] == (LEAD + "\n\n" + TAIL, len(LEAD))
    assert [e["type"] for e in runs[0]] == ["text_delta", "action", "text_delta"]
    assert runs[0][0]["text"] == LEAD
    assert len(env.rows()) == 1 and env.scripts["s"].calls(OUTLINE_PURPOSE) == 1


def test_card_at_is_none_without_a_lead_or_text_before_the_card(tmp_path, env):
    from evowiki.assistant.contracts import MessageIn
    from evowiki.assistant.loop import run_turn
    from evowiki.assistant.session import fold
    from tests.assistant.test_action_tools import INFO
    script(env, blocks={**BLOCKS, "framing": "", "caveat": ""})
    _, deps = first_turn(tmp_path, env)
    ev = [e for e in run_turn(deps, INFO, MessageIn(text="我想养黑壳虾"))
          if e.type in ("text_delta", "action")]
    assert [e.type for e in ev] == ["action", "text_delta"] and ev[1].text == TAIL
    assert fold(INFO, deps.store.entries("s1")).messages[-1].card_at is None


# --- learn_goal: 学点新的 → 开始 runs new_goal itself ------------------------------------------

LEARN = {"text": "复杂网络的渗流\n背景：学过高数，线代", "intent": "learn_goal",
         "goal": "复杂网络的渗流"}


def learn_turn(tmp_path, env, *, durable_deps=False):
    """A learn_goal message: the only model call is the tail."""
    from evowiki.llm.chat import ChatTurn
    from tests.assistant.test_action_tools import chat_turns, turn_deps
    prov = chat_turns(ChatTurn(text=TAIL))
    deps = turn_deps(tmp_path, env, prov, list(env.tools.values()))
    if durable_deps:
        from evowiki.assistant.durable import durable_turn_deps
        deps = durable_turn_deps(deps)
    return prov, deps


def test_a_learn_goal_message_makes_the_blocks_card_with_no_routing_call(tmp_path, env):
    """The bug (user, 2026-10-24): 开始 sent free text and the model lectured instead of calling
    new_goal. A learn_goal message runs new_goal with the form's goal / background before any
    model call; the one model call is the tail (tool_choice="none"), told the tool's result in
    its user message; the lead streams before the card; card_at is the lead's length."""
    from evowiki.assistant.contracts import MessageIn
    from evowiki.assistant.loop import LEARN_GOAL_HEAD, PROMPT_VERSION, run_turn
    from evowiki.assistant.session import fold, session_out
    from tests.assistant.test_action_tools import INFO
    script(env)
    prov, deps = learn_turn(tmp_path, env)
    ev = [e for e in run_turn(deps, INFO, MessageIn(**LEARN))
          if e.type in ("text_delta", "action", "done", "error")]
    assert [e.type for e in ev] == ["text_delta", "action", "text_delta", "done"]
    assert ev[0].text == LEAD and ev[2].text == "\n\n" + TAIL
    assert len(prov.chat_calls) == 1                        # no routing call: only the tail
    tail = prov.chat_calls[0]
    # stage 3: the base version + the Skills the system carries (plan-goal preloaded)
    assert tail.tool_choice == "none" and tail.prompt_version.startswith(PROMPT_VERSION + "+core")
    assert "+plan-goal@" in tail.prompt_version
    assert {m.role for m in tail.messages} == {"user"}      # no synthetic tool-call history
    told = tail.messages[-1].text
    assert told.index("复杂网络的渗流") < told.index(LEARN_GOAL_HEAD)
    assert f"卡片前已经替你对他说了：「{LEAD}」" in told and "1. 认识黑壳虾〔基础〕" in told
    # new_goal got the form's words, as typed
    prompt = env.scripts["s"].requests(OUTLINE_PURPOSE)[0].prompt
    assert "复杂网络的渗流" in prompt and "学过高数，线代" in prompt
    row = env.rows()[0]
    assert (row.content["goal"], row.content["words"]) == ("复杂网络的渗流",
                                                           ["复杂网络的渗流", LEARN["text"]])
    entries = deps.store.entries("s1")
    assert next(e.payload for e in entries if e.type == "user")["intent"] == "learn_goal"
    tool = [e.payload for e in entries if e.type == "tool"]
    assert [(t["name"], t["call_id"], t["ok"]) for t in tool] == [("new_goal", "learn_goal", True)]
    msg = fold(INFO, entries).messages[-1]
    assert msg.card_at == len(LEAD) and msg.text == LEAD + "\n\n" + TAIL
    out = session_out(fold(INFO, entries))
    assert out.messages[0].text == LEARN["text"]   # the bubble
    # how it was sent comes back with it (the plugin hides the blocks card of 学点新的's conversations after a reload)
    assert [m.intent for m in out.messages] == ["learn_goal", None]


def test_a_replayed_learn_goal_turn_emits_the_card_once(tmp_path, env):
    """The same learn_goal turn run again in the same workflow: the new_goal step is recorded (the
    ledger finds its row: no second blocks call), the same events, one card, the same card_at."""
    from evowiki.assistant.contracts import MessageIn
    from evowiki.assistant.loop import run_turn
    from evowiki.assistant.session import fold
    from tests.assistant.test_action_tools import INFO
    script(env)
    runs, answers = [], []
    for i in range(2):
        _, deps = learn_turn(tmp_path / str(i), env, durable_deps=True)
        with durable.virtual_workflow("turn:s1:m1"):
            runs.append([e.model_dump(mode="json")
                         for e in run_turn(deps, INFO, MessageIn(**LEARN), message_id="m1")
                         if e.type in ("text_delta", "action")])
        msg = fold(INFO, deps.store.entries("s1")).messages[-1]
        answers.append((msg.text, msg.card_at))
    assert runs[0] == runs[1] and answers[0] == answers[1] == (LEAD + "\n\n" + TAIL, len(LEAD))
    assert [e["type"] for e in runs[0]] == ["text_delta", "action", "text_delta"]
    assert len(env.rows()) == 1 and env.scripts["s"].calls(OUTLINE_PURPOSE) == 1


def test_a_refused_learn_goal_fails_closed_with_the_refusal_and_no_model_call(tmp_path, env):
    from evowiki.assistant.contracts import MessageIn
    from evowiki.assistant.loop import run_turn
    from tests.assistant.test_action_tools import INFO
    bad = {**BLOCKS, "blocks": [{**BLOCKS["blocks"][0], "level": "很重要"}]}
    for _ in range(3):                                         # the gateway's parse retries too
        env.scripts["s"].push(OUTLINE_PURPOSE, bad)
    prov, deps = learn_turn(tmp_path, env)
    ev = list(run_turn(deps, INFO, MessageIn(**LEARN)))
    err = ev[-1]
    assert err.type == "error" and err.code == "refused" and not err.retryable
    assert "请切换模型重试" in err.detail
    assert prov.chat_calls == [] and env.rows() == []
    assert not any(e.type in ("action", "text_delta", "done") for e in ev)
    logged = [e.payload for e in deps.store.entries("s1") if e.type == "turn_error"]
    assert [p["code"] for p in logged] == ["refused"]


def test_a_learn_goal_turn_without_the_new_goal_tool_is_refused(tmp_path, env):
    from evowiki.assistant.contracts import MessageIn
    from evowiki.assistant.loop import NO_GOAL_TOOL, run_turn
    from tests.assistant.test_action_tools import INFO, chat_turns, turn_deps
    prov = chat_turns()
    deps = turn_deps(tmp_path, env, prov, [t for n, t in env.tools.items() if n != "new_goal"])
    err = list(run_turn(deps, INFO, MessageIn(**LEARN)))[-1]
    assert (err.type, err.code, err.detail) == ("error", "refused", NO_GOAL_TOOL)
    assert prov.chat_calls == []


def test_message_in_learn_goal_shape():
    from evowiki.assistant.contracts import MessageIn, Selection
    assert MessageIn(text="x").intent == "chat"
    for bad in ({"text": "x", "intent": "learn_goal"},                       # no goal
                {"text": "x", "intent": "learn_goal", "goal": "g", "attachments": ["s"]},
                {"text": "x", "intent": "learn_goal", "goal": "g",
                 "selection": Selection(text="t")},
                {"text": "x", "goal": "g"}):                                  # chat with a goal
        with pytest.raises(ValueError):
            MessageIn(**bad)


def test_goal_and_background_have_no_caps_of_their_own():
    """D28: the form took a 2,500-char background and MessageIn refused it at 2,000 (and new_goal
    had other caps again). One contract: the goal and background are parts of the message, whose
    one limit is `text`'s (the form holds 开始 back at the same number)."""
    from evowiki.assistant.contracts import MessageIn
    from evowiki.assistant.tools.actions import NewGoalArgs
    goal, bg = "目" * 1200, "长" * 2500
    assert MessageIn(text="x", intent="learn_goal", goal=goal).goal == goal
    assert NewGoalArgs(goal=goal, words=[bg]).words == [bg]
    with pytest.raises(ValueError):
        MessageIn(text="x" * 4001, intent="learn_goal", goal="g")


def test_new_goal_says_which_step_it_is_on():
    from evowiki.assistant.status_line import blocks_step_line
    from evowiki.goals.blocks import BLOCKS_STEP
    assert blocks_step_line(BLOCKS_STEP) == "正在把目标分成几大块"
    assert blocks_step_line(BLOCKS_STEP, ["蛋白互作"]) == "正在看《蛋白互作》，分成几大块"
    assert blocks_step_line(BLOCKS_STEP, ["甲", "乙"]) == "正在看《甲》等 2 份资料，分成几大块"


def test_new_goal_reports_its_call_as_it_starts(env):
    script(env)
    lines: list[str] = []
    s = env.scripts["s"]
    calls_seen = []

    def say(text):
        lines.append(text)
        calls_seen.append(s.calls(OUTLINE_PURPOSE))
    res = env.tools["new_goal"].run({"goal": GOAL}, CTX.model_copy(update={"progress": say}))
    assert res.error is None, res.content
    assert lines == ["正在把目标分成几大块"]
    assert calls_seen == [0]                          # the line before the call


def test_a_card_edit_keeps_what_the_model_wrote_as_the_blocks_definition():
    from evowiki.bot.executors_assistant import _blocks_of
    [row] = _blocks_of({"blocks": [{"key": "b1", "name": "水温", "definition": "水的温度和它的范围"}]})
    assert row["definition"] == "水的温度和它的范围"


def test_goal_plan_failure_is_localized_and_coded_without_rewriting_history(env, monkeypatch):
    from evowiki.assistant.tools.actions import Refused, _plan_blocks
    from evowiki.goals.intake import IntakeError
    from evowiki.wikiapp import goal as goal_module

    def refuse(*args, **kwargs):
        raise IntakeError("private provider diagnostic")

    monkeypatch.setattr(goal_module, "plan_goal_blocks", refuse)
    snap = env.repo.load("u1")
    for language, expected in (("en", "The model could not organize this goal. Switch models and try again."),
                               ("zh", "模型这次没分好，没法拆这个目标，请切换模型重试")):
        localized = replace(snap, state=snap.state.model_copy(update={"language": language}))
        with pytest.raises(Refused) as got:
            _plan_blocks(localized, CTX, env.deps, "original goal", [], [])
        assert str(got.value) == expected and got.value.code == "goal_plan_failed"
        assert "private provider diagnostic" not in str(got.value)
