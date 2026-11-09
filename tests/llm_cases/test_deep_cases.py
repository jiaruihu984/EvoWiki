"""deep.write (deep@v28, `deep.agent`: the one page writer behind `deep.writer.PageWriter`,
used by wikiapp/organize.py for every page and by deep refine / kchange for rewrites) offline cases.
Scripted gateways and a fake render checker only (tests/deep/fakes.py).

Information flow (the product specification rule WRITER): the page
writer sees the path context (previous / next page), K, the teaching rules, exemplars and the component
catalog. Here: the path line (`wikiapp.refine_apply.path_context`) is in the first message before and
after plan_page for a learning-path page; K's 确定会 / 可能会 cells (+ profile) are `known` — written
by code, never by the model (plan_page has no `known` field) — the page's K is preread, the accepted
plan (with `known`) is in later rounds' prompts, and `read_k` returns the bands; the teaching items in
force are in the first message, in the self-check list and in `read_spec`; at least
one exemplar is listed and readable through `read_spec`; the library catalog is in the write prompt and
`place_component` is offered only with a non-empty library. The writer is never shown a check item
(stem / answer), and cannot write a check section.

Harness: a valid tool loop → a draft with every required section and the self-check recorded; an
a schema-breaking plan goes back to the model as a plan_page error; invalid tool args and unknown tools
go back to the model as errors and the loop recovers; no finish after the last round → `no_draft`
with the last draft kept and the page untouched; an interactive that is not a library id is refused and
one that fails the render check every round is dropped (the text is committed); check sections
cannot be written. Pedagogy (deep@v12): the five behaviour rules are in the self-check list, the cue asks one
specific point; a failing self-check gets exactly one extra round (its items as must-fix), never a
third; the feedback instruction names the three rewrite knobs."""

from dataclasses import replace

import pytest

from evowiki.agent.tools import ToolContext
from evowiki.deep.agent import WRITE_SYSTEM, write_prompt
from evowiki.deep.tools import Workspace, build_tools, check_rules
from evowiki.deep.writer import PageWriter, WriteFailed
from evowiki.goals.contracts import OutlineSection, PageOutline, SectionRole
from evowiki.goals.fold import Page, states_for
from evowiki.llm.prompt_steps import strip_marks
from evowiki.wikiapp.paths import next_path_page
from evowiki.wikiapp.refine_apply import path_context, refine_input
from evowiki.wikiapp.refine_spec import WikiSpecSource
from evowiki.wikiapp.repo import MemoryRepo
from tests.deep.fakes import (
    PLAN,
    DeepScript,
    FakeChecker,
    call,
    finish,
    interactive,
    library,
    planned,
    section,
    self_check,
    simple_round,
    turn,
)
from tests.deep.test_evoui_parity import BASE as BLOCK
from tests.deep.test_writer import outline_input, write_all
from tests.wikiapp.script import Script
from tests.wikiapp.test_goal import NOW, Ids
from tests.wikiapp.test_page import organized
from tests.wikiapp.test_paths import BIG_PATH, big_goal

PURPOSES = ("deep.write",)
CHECK_STEM = "z=-1 表示？"            # the organized page's check item (tests/wikiapp/test_organize)


@pytest.fixture
def env():
    return MemoryRepo(["u1"]), Script(), Ids()


def writer(ds: DeepScript, checker=None, lib="default") -> PageWriter:
    return PageWriter(llm=ds.llm(), checker=checker or FakeChecker(),
                      library=library() if lib == "default" else lib)


def rewrite_input(env, pid):
    return refine_input(env[0].load("u1"), pid, NOW, WikiSpecSource(env[0]))


def requests(ds: DeepScript, purpose: str) -> list:
    return [r for r in ds.requests if r.purpose == purpose]


def tool_errors(ds: DeepScript) -> list[str]:
    """Every tool error the model was sent, once (each request repeats the round's history)."""
    seen = {(r.params.get("round"), m.tool_call_id): m.text for r in requests(ds, "deep.write")
            for m in r.messages if m.role == "tool" and m.is_error}
    return list(seen.values())


def first_message(inp) -> str:
    """The writer's first message before plan_page."""
    return write_prompt(inp, (), Workspace(inp.page))


# ---- inputs: what the writer sees ----

def test_the_first_message_carries_k_outline_teaching_rules_and_exemplars(env):
    inp = outline_input(env, organized(env))
    ds = DeepScript().plan().write(*write_all())
    writer(ds).write(inp)
    p = requests(ds, "deep.write")[0].messages[0].text
    assert inp.known and inp.known in p                       # K (code-written known)
    assert "已经替你读好的这一页格子的 K" in p                   # every band, gaps included
    for s in inp.page.outline.sections:                       # the outline, check section included
        assert f"[{s.id}] {s.heading}" in p
    for item in inp.teaching.items:                           # teaching rules in force
        assert f"[{item.item_id}]" in p
    assert inp.teaching.exemplars and inp.teaching.exemplars[0].title in p   # ≥1 exemplar listed
    assert "这个学习者的表达参数" in p
    assert CHECK_STEM not in p                               # never a check item


def test_write_prompt_carries_plan_known_order_catalog_and_self_check(env):
    inp = outline_input(env, organized(env))
    ds = DeepScript().plan().write(*write_all())
    res = writer(ds, lib=library("pair-demo", "other-demo")).write(inp)
    first = requests(ds, "deep.write")[0]
    text = first.messages[0].text
    assert first.system.startswith(strip_marks(WRITE_SYSTEM))
    assert inp.known == res.outcome.draft.plan.known and inp.known in text   # known is K's
    assert "这些段都要写，按这个顺序：s1, s2, s3" in text
    assert "pair-demo：拼一条链" in text and "other-demo：" in text         # component catalog
    assert "place_component" in {t.name for t in first.tools}
    for rule in check_rules(inp.teaching):                                   # rules → self-check
        assert f"- {rule}:" in text
    assert "EvoUI 索引" in text and CHECK_STEM not in text
    # the system prompt tells the writer to read the spec, the exemplar, the page and K
    assert all(x in WRITE_SYSTEM for x in ("read_spec", "样板页", "read_page", "read_k"))


def test_empty_library_offers_no_place_tool(env):
    inp = outline_input(env, organized(env))
    ds = DeepScript().plan().write(*write_all())
    writer(ds, lib=None).write(inp)
    first = requests(ds, "deep.write")[0]
    assert "交互组件库是空的" in first.messages[0].text
    assert "place_component" not in {t.name for t in first.tools}


def test_tool_results_give_spec_exemplar_page_and_k(env):
    inp = outline_input(env, organized(env))
    ws = Workspace(inp.page, page_cells=inp.page_cells, required=inp.required)
    tools = {t.spec.name: t for t in build_tools(
        ws, user_id=inp.user_id, page_id=inp.page_id, title=inp.title, kview=inp.kview,
        names=inp.names, teaching=inp.teaching, checker=FakeChecker(), known=inp.known)}
    ctx = ToolContext(user_id=inp.user_id, session_id="t", page_id=inp.page_id)
    items = tools["read_spec"].run({"part": "items"}, ctx).content
    assert all(f"[{i.item_id}]" in items for i in inp.teaching.items)
    exemplar = tools["read_spec"].run({"part": "exemplar"}, ctx).content
    assert exemplar and exemplar[:200] == inp.teaching.exemplars[0].text[:200]
    page = tools["read_page"].run({}, ctx).content
    assert all(s.id in page for s in inp.page.outline.sections)
    concepts = sorted({c.split(".", 1)[0] for c in inp.page_cells})
    k = tools["read_k"].run({"concepts": concepts}, ctx)
    assert k.error is None and all(inp.names[c] in k.content for c in concepts)


def _path_page_input(env, index: int):
    """The `index`-th (1-based) page of the 3-page statistics path, with a one-section outline."""
    repo, _, ids = env
    res, _ = big_goal(env)
    pid = None
    for _ in range(index):
        pid = next_path_page(repo, "u1", res.path_id, now=NOW, new_id=ids).page_id
    snap = repo.load("u1")
    rec = snap.state.pages[pid]
    outline = PageOutline(page_id=pid, goal=rec.goal, concepts=rec.concepts, targets=rec.targets,
                          sections=(OutlineSection(id="s1", heading="先看问题",
                                                   role=SectionRole.EXPLAIN),))
    page = Page(outline=outline, states=states_for(outline), texts={}, snapshot={},
                unresolved_conflicts=(), conflicts=())
    return refine_input(snap, pid, NOW, WikiSpecSource(repo), page=page, mode="page")


def test_path_page_sees_its_previous_and_next_page(env):
    inp = _path_page_input(env, 2)
    objectives = [p["objective"] for p in BIG_PATH["pages"]]
    assert inp.path.startswith("学习路径（第 2/3 页）")
    assert f"上一页《{objectives[0]}》" in inp.path and f"下一页《{objectives[2]}》" in inp.path
    assert inp.path in first_message(inp)
    assert inp.path in write_prompt(inp, (), planned(inp))
    assert "上一页讲过的只提一句" in inp.path
    assert "下一页的内容这里不展开" in inp.path
    assert "没有后续页面" not in inp.path


def test_path_line_links_pages_that_exist_only(env):
    """Page 2: page 1 was created on the way (its link is given), page 3 does not exist yet (no
    link, never an invented id)."""
    inp = _path_page_input(env, 2)
    st = env[0].load("u1").state
    rec = next(r for r in st.paths.values() if inp.page_id in r.page_ids)
    first = rec.page_ids[0]
    assert f"[{st.pages[first].title}](#p:{first})" in inp.path
    assert inp.path.count("#p:") == 1 and "用给出的链接" in inp.path


def test_first_path_page_has_no_previous(env):
    inp = _path_page_input(env, 1)
    assert "这是第一页" in inp.path and "上一页《" not in inp.path
    assert f"下一页《{BIG_PATH['pages'][1]['objective']}》" in inp.path


def test_last_path_page_finishes_its_goal_without_promising_a_next_page(env):
    inp = _path_page_input(env, 3)
    assert "这是最后一页" in inp.path
    assert "上一页讲过的只提一句" in inp.path
    assert "没有后续页面，本页目标在本页讲完" in inp.path
    assert "下一页的内容这里不展开" not in inp.path
    assert inp.path in first_message(inp)
    assert inp.path in write_prompt(inp, (), planned(inp))


def test_single_path_page_has_no_neighbor_deferral(env):
    inp = _path_page_input(env, 1)
    state = env[0].load("u1").state
    key, record = next((key, r) for key, r in state.paths.items() if inp.page_id in r.page_ids)
    path = record.path.model_copy(update={"pages": record.path.pages[:1]})
    record = record.model_copy(update={"path": path, "page_ids": record.page_ids[:1]})
    state = state.model_copy(update={"paths": {**state.paths, key: record}})
    context = path_context(state, inp.page_id)
    assert "第 1/1 页" in context and "这是该路径唯一一页" in context
    assert "没有后续页面，本页目标在本页讲完" in context
    assert "上一页讲过的只提一句" not in context
    assert "下一页的内容这里不展开" not in context
    inp = replace(inp, path=context)
    for prompt in (first_message(inp), write_prompt(inp, (), planned(inp))):
        assert "没有后续页面，本页目标在本页讲完" in prompt
        assert "如果存在后续页，各页沿用它" in prompt
        assert "后面各页一直用它" not in prompt


def test_page_on_no_path_has_no_path_line(env):
    inp = outline_input(env, organized(env))
    assert inp.path == ""
    assert "学习路径（" not in first_message(inp)


# ---- harness ----

def test_valid_tool_loop_gives_a_draft_with_every_required_section(env):
    inp = outline_input(env, organized(env))
    ds = DeepScript().plan().write(turn(call("read_spec", part="items"),
                                        call("read_page"), call("read_k", concepts=["c1"])),
                                   *write_all())
    res = writer(ds).write(inp)
    assert [s.section_id for s in res.outcome.draft.sections] == ["s1", "s2", "s3"]
    assert all(s.body.strip() for s in res.outcome.draft.sections)
    assert {f.rule for f in res.outcome.self_check} == set(check_rules(inp.teaching))
    assert res.outcome.rounds == 1 and ds.pending() == {}
    assert {p for _, p in ds.served} == set(PURPOSES)


@pytest.mark.parametrize("bad", [{**PLAN, "known": "他什么都会"}, {"observable": "x"}])
def test_an_unusable_plan_goes_back_to_the_model(env, bad):
    """A `known` the model tries to write, or missing answers: plan_page refuses, nothing is
    written until a plan is accepted, and `known` stays K's."""
    inp = outline_input(env, organized(env))
    ds = DeepScript().plan(bad).write(turn(section("s1", role="organizer", depth="quick"))) \
        .plan().write(*write_all())
    res = writer(ds).write(inp)
    errors = tool_errors(ds)
    assert any("参数不合法" in e for e in errors) and any("先调用 plan_page" in e for e in errors)
    assert res.outcome.draft.plan.known == inp.known


def test_invalid_tool_args_and_unknown_tools_go_back_to_the_model(env):
    inp = rewrite_input(env, organized(env))
    ds = DeepScript().plan().write(
        turn(call("write_section", section_id="s2", heading="h", role="explain"),   # no body / cue
             call("write_section", 2, **{**section("s2")["args"], "depth": "very-deep"}),
             call("read_spec", part="items", extra=1),
             call("write_html", html="<p>")),
        *simple_round())
    res = writer(ds).write(inp)
    errors = tool_errors(ds)
    assert len(errors) == 4 and any("没有这个工具：write_html" in e for e in errors)
    assert [s.section_id for s in res.outcome.draft.sections] == ["s2"]


def test_no_finish_after_the_last_round_is_no_draft_and_the_page_is_kept(env):
    from tests.deep.test_service import record, refine, service

    pid = organized(env)
    before = record(env, pid)
    ds = DeepScript().plan().write(turn(section("s2")), *[turn(text="写完了")] * 9)
    res = refine(service(env, ds), pid)
    assert (res.job.stage, res.job.failure) == ("failed", "no_draft")
    assert record(env, pid) == before                                   # skeleton kept
    assert [s.section_id for s in res.last_draft.sections] == ["s2"]
    # the same through the page writer: the last draft rides on the failure, nothing committed
    ds = DeepScript().plan().write(turn(section("s2")), *[turn(text="写完了")] * 9)
    with pytest.raises(WriteFailed) as e:
        writer(ds).write(rewrite_input(env, pid))
    assert e.value.failure == "no_draft" and e.value.draft.sections[0].section_id == "s2"


def test_an_interactive_not_in_the_library_is_refused(env):
    inp = rewrite_input(env, organized(env))
    body = "s2 正文\n\n{{interactive:free-html}}\n"
    ds = DeepScript().plan().write(turn(section("s2", body=body), interactive("free-html")),
                                   turn(finish()),                  # refused: unknown placeholder
                                   *simple_round(2))
    res = writer(ds).write(inp)
    errors = tool_errors(ds)
    assert any("组件库里没有 free-html" in e for e in errors)
    assert any("不存在的交互" in e for e in errors)
    assert res.outcome.draft.interactives == () and "free-html" not in \
        res.outcome.draft.sections[0].body


def test_an_interactive_failing_the_render_check_is_dropped_without_another_round(env):
    """Stage 4 T4: a render failure never costs a round — the interactive is dropped at finish."""
    inp = rewrite_input(env, organized(env))
    ds = DeepScript().plan().write(*simple_round(1, with_interactive=True))
    res = writer(ds, FakeChecker(lambda d: "按钮点了没反应")).write(inp)
    [d] = res.outcome.dropped
    assert d.interactive_id == "pair-demo" and "按钮点了没反应" in d.reason
    assert res.outcome.draft.interactives == () and res.outcome.rounds == 1
    assert ds.pending() == {} and sorted(_round_prompts(ds)) == [0]
    assert "{{interactive:" not in res.outcome.draft.sections[0].body


def test_a_block_failing_the_render_check_is_dropped_without_another_round(env):
    inp = rewrite_input(env, organized(env))
    body = "s2 正文。\n\n{{block:pair}}\n"
    ds = DeepScript().plan().write(
        turn(section("s2", body=body),
             call("write_block", block_id="pair", section_id="s2", why="动手看", block=BLOCK)),
        turn(finish()))
    res = writer(ds, FakeChecker(fail_block=lambda bid, b: "溢出")).write(inp)
    assert [x.interactive_id for x in res.outcome.dropped] == ["pair"]
    assert res.outcome.rounds == 1 and ds.pending() == {}
    assert res.outcome.draft.blocks == () and "{{block:" not in res.outcome.draft.sections[0].body


# ---- pedagogy self-check rules, the extra self-check round, the cue rule, the rewrite knobs ----

DIMENSIONS = ("accuracy", "coverage", "fits_learner", "coherence", "useful", "structure")
C04_FIX = "coherence: 「碱基」先用后讲，已改（段 s2）"      # tests/deep/fakes.self_check(ok=False)


def _round_prompts(ds: DeepScript) -> dict[int, str]:
    """The first user message of each write round, by round index."""
    out: dict[int, str] = {}
    for r in requests(ds, "deep.write"):
        out.setdefault(r.params["round"], r.messages[0].text)
    return out


def test_write_prompt_lists_the_five_behaviours_the_cue_rule_and_facts_are_not_self_checked(env):
    inp = outline_input(env, organized(env))
    text = first_message(inp)
    rules = check_rules(inp.teaching)
    # deep@v31: the six dimensions (the five behaviours are covered by them)
    for key in DIMENSIONS:
        assert f"- {key}: {rules[key]}" in text
    assert "知识准确" in rules["accuracy"] and "几天后单独看到也能读懂" in WRITE_SYSTEM
    assert "读到它的人能想起这一段讲的是什么" not in WRITE_SYSTEM


def test_a_failing_self_check_gets_exactly_one_extra_round_with_it_as_must_fix(env):
    inp = rewrite_input(env, organized(env))
    ds = DeepScript().plan().write(turn(section("s2")), turn(finish(checks=self_check(ok=False))),
                                   turn(section("s2", 2, body="s2 改过：先讲印章再讲碱基。")),
                                   turn(finish(2)))
    res = writer(ds).write(inp)
    prompts = _round_prompts(ds)
    assert sorted(prompts) == [0, 1] and res.outcome.rounds == 2 and ds.pending() == {}
    must_fix = prompts[1].split("上一轮必须改的问题：\n")[1].split("\n\n")[0]
    assert must_fix == f"- {C04_FIX}"                                  # only the failing item
    assert "s2 改过" in res.outcome.draft.sections[0].body             # the extra round's draft
    assert all(f.ok for f in res.outcome.self_check)


def test_a_second_failing_self_check_does_not_cause_a_third_round(env):
    inp = rewrite_input(env, organized(env))
    bad = self_check(ok=False)
    ds = DeepScript().plan().write(turn(section("s2")), turn(finish(checks=bad)),
                                   turn(section("s2", 2)), turn(finish(2, checks=bad)))
    res = writer(ds).write(inp)
    assert sorted(_round_prompts(ds)) == [0, 1] and res.outcome.rounds == 2
    assert ds.pending() == {}                                         # no third round asked for
    assert [f.rule for f in res.outcome.self_check if not f.ok] == ["coherence"]   # recorded as it is


def test_no_extra_round_when_the_self_check_is_all_ok(env):
    inp = rewrite_input(env, organized(env))
    ds = DeepScript().plan().write(*simple_round())
    res = writer(ds).write(inp)
    assert sorted(_round_prompts(ds)) == [0] and res.outcome.rounds == 1


def test_the_extra_round_ending_without_finish_keeps_the_accepted_draft(env):
    inp = rewrite_input(env, organized(env))
    ds = DeepScript().plan().write(turn(section("s2")), turn(finish(checks=self_check(ok=False))),
                                   turn(section("s2", 2, body="半截改写")),
                                   *[turn(text="写完了")] * 3)
    res = writer(ds).write(inp)
    assert res.outcome.rounds == 2 and "半截改写" not in res.outcome.draft.sections[0].body
    assert [f.rule for f in res.outcome.self_check if not f.ok] == ["coherence"]


def test_a_self_check_round_failing_the_render_check_keeps_its_text_and_drops_the_block(env):
    """Round 1 passes the render check with a failing self-check; the extra round 2 adds a block
    that fails: round 2's draft is kept and only the block is dropped (an interactive never undoes
    a round's text)."""
    inp = rewrite_input(env, organized(env))
    body = "s2 精修正文。\n\n{{interactive:pair-demo}}\n"
    ds = DeepScript().plan().write(
        turn(call("read_spec", part="items"), section("s2", body=body), interactive()),
        turn(finish(checks=self_check(ok=False))),
        turn(section("s2", 2, body=body + "\n{{block:pair}}\n"),
             call("write_block", 2, block_id="pair", section_id="s2", why="动手看", block=BLOCK)),
        turn(finish(2)))
    res = writer(ds, FakeChecker(fail_block=lambda bid, b: "溢出")).write(inp)
    assert res.outcome.rounds == 2 and ds.pending() == {}
    assert [d.interactive_id for d in res.outcome.dropped] == ["pair"]
    assert res.outcome.draft.blocks == ()
    assert [i.interactive_id for i in res.outcome.draft.interactives] == ["pair-demo"]
    assert "{{block:" not in res.outcome.draft.sections[0].body
    assert [f.rule for f in res.outcome.self_check if not f.ok] == []      # round 2's self-check


@pytest.mark.parametrize("kind", ["deeper", "other"])
def test_feedback_instruction_names_the_three_knobs_and_their_setting(env, kind):
    from dataclasses import replace

    from evowiki.goals.rewrite_knobs import REWRITE_KNOBS, feedback_instruction

    base = rewrite_input(env, organized(env))
    inp = replace(base, mode="feedback", scope=("s2",),
                  instruction=feedback_instruction(kind, "配对规则怎么来"))
    text = first_message(inp)
    for name, setting in REWRITE_KNOBS[kind].items():
        assert f"{name}（" in text and setting in text
    assert inp.instruction in write_prompt(inp, (), planned(inp))
    if kind == "deeper":
        assert "拆得更细" in text and "补上推导" in text and "往下补上" in text
    else:
        assert "保持现在的深度" in text and "换一个切入角度" in text


def test_check_sections_cannot_be_written(env):
    inp = rewrite_input(env, organized(env))
    ds = DeepScript().plan().write(
        turn(section("s4", heading="改题"), section("s9", 2, role="check", after="s3")),
        *simple_round())
    res = writer(ds).write(inp)
    errors = tool_errors(ds)
    assert any("确认题段不能" in e for e in errors)              # the outline's check section
    assert len(errors) == 2                                      # role=check is not even an option
    assert {s.section_id for s in res.outcome.draft.sections} == {"s2"}
