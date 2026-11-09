"""读懂这篇 from the chat (reading-mode spec §3.1, §3.3, §8.1 「入口」; plan stage 4 Task 7):
`new_goal` runs S1's understand mode in the assistant's turn (one goals.intake call, the learner's
metered gateway) and proposes a draft with the prerequisite `blocks` and the `reading` plan;
`edit_reading` merges / skips / restores units with no call; confirming makes the goal through
`wikiapp.reading.create_reading_goal_with_source` with no S1 call and never through the learning
goal flow (create_goal_from_blocks). One paper and no words: ask
first; two materials: MSG_UNDERSTAND_ONE. Real Postgres (the paper imported inline), the real
ledger + executors, a SCRIPTED LLM — no paid call."""

from dataclasses import replace
from itertools import count

import pytest

from evowiki.assistant.tools.actions import (
    ASK_USE,
    NO_READING_DRAFT,
    READING_CONFIRMED,
    READING_FAILED,
    REPLACED,
    build_action_tools,
)
from evowiki.goals.intake import OUTLINE_PURPOSE
from evowiki.retrieval.embed import HashingEmbedder
from evowiki.sources import store as source_store
from evowiki.sources.importer import ImportDeps
from evowiki.sources.reader import PooledSourceReader
from evowiki.sources.vision import NoVision
from evowiki.wikiapp import goal as goal_mod
from evowiki.wikiapp import goal_sources
from evowiki.wikiapp.events import GoalMapSaved, ReadingSaved
from evowiki.wikiapp.goal_sources import MSG_UNDERSTAND_ONE
from tests.assistant.fakes import ungrounded
from tests.assistant.test_action_tools import CTX
from tests.assistant.test_goal_blocks_tools import BLOCKS, env  # noqa: F401 - the fixture
from tests.sources.test_importer import add_source, run
from tests.wikiapp.goal_script import PROBE
from tests.wikiapp.test_path_edits import NOW

pytestmark = pytest.mark.usefixtures("every_skill")   # tools called without a load (stage 3)

PAPER = ("# 摘要\n\n我们提出一种新结构。\n\n# 1 引言\n\n序列建模很重要。\n\n# 2 方法\n\n"
         "模型由编码器组成。\n\n# 参考文献\n\n[1] 某某。\n")
BOOK = "# 1 向量\n\n向量是有方向的量。\n\n## 1.1 向量加法\n\n首尾相接。\n"
READ = {"title": "读懂这篇论文", "framing": "先补一个前置，再分两段读。",
        "mainline": "先懂编码器，再读方法。", "start": ["编码器"], "question": "",
        "blocks": [{"name": "编码器", "summary": "把输入变成向量", "covers": ["向量"],
                    "atomic": True}],
        "units": [{"title": "问题和动机", "about": "为什么要新结构",
                   "spans": [{"section": "§摘要"}, {"section": "§1"}]},
                  {"title": "方法", "spans": [{"section": "§2"}]}],
        "left_out": ["§参考文献"]}


def imported(pool, sid: str, text: str) -> None:
    ids = count(1)              # ids of its own: two imports in one test never collide
    deps = ImportDeps(pool=pool, embedder=HashingEmbedder(), llm_for=lambda _u: None,
                      local=NoVision(), claude=NoVision(), assistant_sees_images=lambda _u: False,
                      clock=lambda: NOW, new_id=lambda: f"{sid}-{next(ids)}")
    add_source(pool, sid, text.encode(), kind="md")
    assert run(deps, sid)[-1][0] == "done"
    with pool.connection() as conn:          # a title the card and S1's goal text can name
        conn.execute("update source set title = %s where source_id = %s", (sid, sid))


@pytest.fixture
def paper(env):  # noqa: F811
    imported(env.pool, "paper", PAPER)
    return env


def tools(env):  # noqa: F811
    reader = PooledSourceReader(env.pool, HashingEmbedder())
    return {t.spec.name: t for t in ungrounded(build_action_tools(
        replace(env.deps, sources=reader)))}


def propose(env, **args):  # noqa: F811
    env.scripts["s"].push(OUTLINE_PURPOSE, READ)
    res = tools(env)["new_goal"].run({"goal": "帮我读懂这篇", "source_ids": ["paper"], **args},
                                     CTX)
    assert res.error is None, res.content
    return res, env.ledger.get("u1", res.action.action_id)


def no_learning_flow(monkeypatch):
    """A 读懂 goal is never made by the learning goal flow (§8.1 入口)."""
    def refuse(*_a, **_k):
        raise AssertionError("a reading goal went through the learning goal flow")
    for mod, names in ((goal_mod, ("create_goal_from_blocks",)),
                       (goal_sources, ("create_goal_from_blocks",
                                       "create_goal_from_blocks_with_sources"))):
        for name in names:
            monkeypatch.setattr(mod, name, refuse)
    import evowiki.bot.executors_assistant as ex
    monkeypatch.setattr(ex, "create_goal_from_blocks_with_sources", refuse)


# --- the draft ------------------------------------------------------------------------------------

def test_understand_with_one_paper_plans_the_reading_in_this_turn(paper):
    lines: list[str] = []
    paper.scripts["s"].push(OUTLINE_PURPOSE, READ)
    res = tools(paper)["new_goal"].run({"goal": "帮我读懂这篇", "source_ids": ["paper"]},
                                       CTX.model_copy(update={"progress": lines.append}))
    assert res.error is None, res.content
    s = paper.scripts["s"]
    assert s.calls(OUTLINE_PURPOSE) == 1 and s.calls() == 1          # one S1 call, this turn
    prompt = s.requests(OUTLINE_PURPOSE)[0].prompt
    assert "学习目标：「读懂《paper》」" in prompt and '"units"' in prompt
    assert lines == ["正在看《paper》", "正在核对段落"]
    row = paper.ledger.get("u1", res.action.action_id)
    c = row.content
    assert (row.kind, row.status, c["use"], c["source_ids"]) == (
        "goal_new", "proposed", "understand", ["paper"])
    assert [b["name"] for b in c["blocks"]] == ["编码器"] and c["blocks"][0]["target"] is False
    assert c["pages"] == [{"key": "b1", "title": "编码器", "covers": "向量", "level": ""}]
    r = c["reading"]
    assert r["source_id"] == "paper" and len(r["left_out"]) == 1
    assert [(u["order"], u["title"], len(u["spans"])) for u in r["units"]] == [
        (1, "问题和动机", 2), (2, "方法", 1)]
    assert all(u["unit_id"].startswith("ru") for u in r["units"])
    assert "limits" in c and c["mainline"] == "先懂编码器，再读方法。"
    # the card line (§3.3); reasons only in the ? (why)
    assert row.summary == "先补这几个：编码器；然后按原文分 2 段读：1 问题和动机 · 2 方法"
    assert res.action.title == row.summary and "按《paper》的内容排的" in row.why
    assert [i.title for i in res.action.items] == ["编码器"]
    assert "按原文读：" in res.content and "2. 方法" in res.content
    assert paper.state().maps == {} or all(
        m.goal != c["goal"] for m in paper.state().maps.values())     # nothing written yet


def test_a_text_first_paper_is_parsed_before_the_units_are_planned(paper):
    """The units attach to section ids: the wait comes first, the plan reads what it left."""
    with paper.pool.connection() as conn:
        conn.execute("update source set text_first = true where source_id = 'paper'")
    waits: list[bool] = []

    def wait(_uid, ids, parse, say):
        assert ids == ["paper"] and paper.scripts["s"].calls(OUTLINE_PURPOSE) == 0
        waits.append(parse)
        if parse:
            say("正在读《paper》的版面，0/15 页")
            with paper.pool.connection() as conn:
                conn.execute("update source set text_first = false where source_id = 'paper'")
    paper.scripts["s"].push(OUTLINE_PURPOSE, READ)
    reader = PooledSourceReader(paper.pool, HashingEmbedder())
    new_goal = {t.spec.name: t for t in ungrounded(build_action_tools(
        replace(paper.deps, sources=reader, await_materials=wait)))}["new_goal"]
    lines: list[str] = []
    res = new_goal.run({"goal": "帮我读懂这篇", "source_ids": ["paper"]},
                       CTX.model_copy(update={"progress": lines.append}))
    assert res.error is None, res.content
    assert waits == [False, True] and lines[0] == "正在读《paper》的版面，0/15 页"


def test_a_question_back_to_him_needs_no_parse(paper):
    with paper.pool.connection() as conn:
        conn.execute("update source set text_first = true where source_id = 'paper'")
    waits: list[bool] = []
    reader = PooledSourceReader(paper.pool, HashingEmbedder())
    new_goal = {t.spec.name: t for t in ungrounded(build_action_tools(replace(
        paper.deps, sources=reader,
        await_materials=lambda _u, _i, parse, _s: waits.append(parse))))}["new_goal"]
    res = new_goal.run({"goal": "", "source_ids": ["paper"]}, CTX)
    assert res.error is not None and ASK_USE in res.content and waits == [False]


def test_one_paper_and_no_words_asks_then_his_answer_picks_the_draft(paper):
    s = paper.scripts["s"]
    res = tools(paper)["new_goal"].run({"source_ids": ["paper"]}, CTX)
    assert res.error is not None and ASK_USE in res.content
    assert s.calls() == 0 and paper.rows() == []
    s.push(OUTLINE_PURPOSE, READ)
    out = tools(paper)["new_goal"].run({"source_ids": ["paper"], "use": "understand"}, CTX)
    assert out.error is None, out.content
    assert "reading" in paper.ledger.get("u1", out.action.action_id).content
    s.push(OUTLINE_PURPOSE, BLOCKS)
    out = tools(paper)["new_goal"].run({"source_ids": ["paper"], "use": "follow"}, CTX)
    assert out.error is None, out.content
    c = paper.ledger.get("u1", out.action.action_id).content
    assert "reading" not in c and c["use"] == "follow" and len(c["blocks"]) == 3
    assert '"units"' not in s.requests(OUTLINE_PURPOSE)[-1].prompt


def test_units_refused_after_the_repairs_say_so_and_leave_nothing(paper):
    """Forced structural failure: S1 and both repairs name a section the paper does not have."""
    s = paper.scripts["s"]
    bad = [{"title": "甲", "spans": [{"section": "§7"}]}]
    s.push(OUTLINE_PURPOSE, {**READ, "units": bad})
    s.push("goals.reading.repair", {"units": bad})
    s.push("goals.reading.repair", {"units": bad + bad})
    lines: list[str] = []
    res = tools(paper)["new_goal"].run({"goal": "帮我读懂这篇", "source_ids": ["paper"]},
                                       CTX.model_copy(update={"progress": lines.append}))
    assert res.error is not None and READING_FAILED in res.content and "不要再调" in res.content
    assert s.calls(OUTLINE_PURPOSE) == 1 and s.calls("goals.reading.repair") == 2
    assert lines == ["正在看《paper》", "正在核对段落", "正在修正段落", "正在核对段落",
                     "正在修正段落", "正在核对段落"]
    assert paper.rows() == []


def test_units_a_repair_fixes_make_the_draft(paper):
    s = paper.scripts["s"]
    s.push(OUTLINE_PURPOSE, {**READ, "units": [{"title": "甲", "spans": [{"section": "§7"}]}]})
    s.push("goals.reading.repair", {"units": READ["units"]})
    res = tools(paper)["new_goal"].run({"goal": "帮我读懂这篇", "source_ids": ["paper"]}, CTX)
    assert res.error is None, res.content
    c = paper.ledger.get("u1", res.action.action_id).content
    assert [u["title"] for u in c["reading"]["units"]] == ["问题和动机", "方法"]
    assert c["framing"] == READ["framing"]


def test_two_materials_and_understand_is_refused(paper):
    imported(paper.pool, "book", BOOK)
    res = tools(paper)["new_goal"].run(
        {"goal": "帮我读懂这篇", "source_ids": ["paper", "book"]}, CTX)
    assert res.error is not None and MSG_UNDERSTAND_ONE in res.content
    assert paper.scripts["s"].calls() == 0 and paper.rows() == []


# --- edit_reading -------------------------------------------------------------------------------

def test_edit_reading_merges_skips_and_restores_with_no_call(paper):
    _, old = propose(paper)
    first, second = (u["unit_id"] for u in old.content["reading"]["units"])
    calls = paper.scripts["s"].calls()
    res = tools(paper)["edit_reading"].run({"ops": [{"op": "merge", "units": [1, 2]}]}, CTX)
    assert res.error is None, res.content
    assert paper.scripts["s"].calls() == calls                         # no model call
    new = paper.ledger.get("u1", res.action.action_id)
    assert paper.ledger.get("u1", old.action_id).status == "rejected"
    assert paper.ledger.get("u1", old.action_id).detail == REPLACED
    units = new.content["reading"]["units"]
    assert [(u["unit_id"], u["title"]) for u in units] == [(first, "问题和动机、方法")]
    assert second not in str(new.content["reading"])                  # the second id is gone
    assert new.summary == "先补这几个：编码器；然后按原文分 1 段读：1 问题和动机、方法"
    assert new.content["blocks"] == old.content["blocks"]
    assert "合成了一段" in res.content
    # skip and restore on a fresh draft; an op that cannot be done is said next to the others
    propose(paper)
    res = tools(paper)["edit_reading"].run({"ops": [
        {"op": "skip", "units": [2]}, {"op": "unskip", "units": [1]}]}, CTX)
    assert res.error is None, res.content
    c = paper.ledger.get("u1", res.action.action_id).content
    assert [u["skipped"] for u in c["reading"]["units"]] == [False, True]
    assert "1 段读：1 问题和动机" in paper.ledger.get("u1", res.action.action_id).summary
    assert c["refused"] == ["恢复第 1 段（这一段没有设成不读）"]
    res = tools(paper)["edit_reading"].run({"ops": [{"op": "unskip", "units": [2]}]}, CTX)
    c = paper.ledger.get("u1", res.action.action_id).content
    assert [u["skipped"] for u in c["reading"]["units"]] == [False, False]


def test_edit_reading_refuses_what_it_cannot_do(paper):
    t = tools(paper)["edit_reading"]
    assert NO_READING_DRAFT in t.run({"ops": [{"op": "skip", "units": [1]}]}, CTX).content
    _, row = propose(paper)
    out = t.run({"ops": [{"op": "merge", "units": [1, 3]}, {"op": "skip", "units": [9]}]}, CTX)
    assert out.error is not None and "这些改动都做不了" in out.content
    assert paper.ledger.approve("u1", row.action_id).status == "executed"
    out = t.run({"ops": [{"op": "skip", "units": [1]}]}, CTX)
    assert out.error is not None and out.content.endswith(READING_CONFIRMED)


def test_edit_goal_blocks_on_a_reading_draft_keeps_its_units(paper):
    _, old = propose(paper)
    res = tools(paper)["edit_goal_blocks"].run(
        {"ops": [{"op": "add", "name": "向量", "summary": "有方向的量"}]}, CTX)
    assert res.error is None, res.content
    new = paper.ledger.get("u1", res.action.action_id)
    assert new.content["reading"] == old.content["reading"]
    assert new.summary.startswith("先补这几个：编码器、向量；然后按原文分 2 段读")


# --- confirm -------------------------------------------------------------------------------------

def test_confirm_makes_the_reading_goal_with_no_s1_call(paper, monkeypatch):
    _, row = propose(paper)
    units = [u["unit_id"] for u in row.content["reading"]["units"]]
    no_learning_flow(monkeypatch)
    s = paper.scripts["s"]
    calls, probes = s.calls(), s.calls(PROBE)
    done = paper.ledger.approve("u1", row.action_id)
    assert done.status == "executed", done.detail
    # no S1 (goals.intake) at confirm, and no model call at all: the opening diagnosis only picks
    # (stage 3 T11: the assistant writes its question in the chat)
    assert s.calls() - calls == s.calls(PROBE) - probes
    st = paper.state()
    (reading,) = st.readings.values()
    assert [u.unit_id for u in reading.units] == units
    m = st.maps[reading.map_id]
    assert [n.name for n in m.nodes] == ["编码器"] and m.sources == ("paper",)
    assert not any(u in {n.id for n in m.nodes} for u in units)
    events = [e.body for e in paper.repo.wiki_events_of("u1", {"reading_saved",
                                                                 "goal_map_saved"})]
    assert any(isinstance(e, ReadingSaved) and e.action == "planned" for e in events)
    assert any(isinstance(e, GoalMapSaved) for e in events)
    with paper.pool.connection() as conn:
        uses = [(u["use"], u["goal_id"]) for u in source_store.uses_of(conn, "u1", "paper")]
        links = source_store.links_for_sources(conn, "u1", ["paper"])
    assert ("understand", reading.map_id) in uses
    assert links == []                                  # no reads; teaches only once entered
    undone = tools(paper)["undo"].run({"action_id": row.action_id}, CTX)
    assert undone.error is None and undone.action.status == "undone"


def test_a_reading_row_for_another_material_is_refused(paper):
    _, row = propose(paper)
    bad = {**row.content, "source_ids": ["other"]}
    with paper.pool.connection() as conn:
        conn.execute("update agent_action set content = %s::jsonb where action_id = %s",
                     (__import__("json").dumps(bad), row.action_id))
    done = paper.ledger.approve("u1", row.action_id)
    assert done.status == "failed" and "带读" in done.detail


def test_undo_of_a_reading_goal_whose_opening_asks_nothing_archives_it(paper):
    """No prerequisite: no probe, no draft stored — the undo still archives the goal by the map
    id written at execute (I2)."""
    paper.scripts["s"].push(OUTLINE_PURPOSE, {**READ, "blocks": [], "start": []})
    res = tools(paper)["new_goal"].run({"goal": "帮我读懂这篇", "source_ids": ["paper"]}, CTX)
    assert res.error is None, res.content
    row = paper.ledger.get("u1", res.action.action_id)
    assert row.content["blocks"] == []
    done = paper.ledger.approve("u1", row.action_id)
    assert done.status == "executed", done.detail
    st = paper.state()
    (reading,) = st.readings.values()
    assert st.maps[reading.map_id].nodes == () and not st.drafts
    assert done.undo["map_id"] == reading.map_id and done.undo["reading_id"] == reading.reading_id
    undone = tools(paper)["undo"].run({"action_id": row.action_id}, CTX)
    assert undone.error is None and undone.action.status == "undone", undone.content
    assert paper.state().goal_settings[reading.map_id].archived


def test_dropping_the_last_prerequisite_of_a_reading_draft_is_allowed(paper):
    """I3: a reading needs no prerequisite — the chat edit and the card's × both drop the last one;
    the card line then says 不用先补什么. A learning draft still keeps at least one block."""
    propose(paper)
    res = tools(paper)["edit_goal_blocks"].run({"ops": [{"op": "drop", "name": "编码器"}]}, CTX)
    assert res.error is None, res.content
    new = paper.ledger.get("u1", res.action.action_id)
    assert new.content["blocks"] == [] and new.content["reading"]
    assert new.summary.startswith("不用先补什么；然后按原文分 2 段读")
    _, row = propose(paper)
    kept = paper.ledger.edit_draft("u1", row.action_id, {"pages": []})
    assert kept.content["blocks"] == [] and kept.content["pages"] == []
    done = paper.ledger.approve("u1", row.action_id)
    assert done.status == "executed", done.detail
    st = paper.state()
    assert all(st.maps[r.map_id].nodes == () for r in st.readings.values())


def test_a_reading_already_begun_is_refused_before_s1(paper):
    """M4/M3: the same paper's reading with a unit entered — new_goal refuses it with no call."""
    from evowiki.wikiapp.goal import GOAL_IN_PROGRESS, _Work
    _, row = propose(paper)
    assert paper.ledger.approve("u1", row.action_id).status == "executed"
    (reading,) = paper.state().readings.values()
    seen = reading.model_copy(update={"units": (
        reading.units[0].model_copy(update={"analyzed": True}), *reading.units[1:])})
    w = _Work(paper.repo.load("u1"), None, "u1", now=NOW, new_id=lambda p: f"{p}-t")
    w.add(ReadingSaved(record=seen, action="unit_analyzed"))
    w.commit(paper.repo)
    calls = paper.scripts["s"].calls()
    res = tools(paper)["new_goal"].run({"goal": "帮我读懂这篇", "source_ids": ["paper"]}, CTX)
    assert res.error is not None and GOAL_IN_PROGRESS in res.content
    assert paper.scripts["s"].calls() == calls
