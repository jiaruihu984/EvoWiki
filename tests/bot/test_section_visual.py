"""Scoped visual drafts use the page writer, preview assets and revision approval/undo."""

from types import SimpleNamespace

import pytest

from evowiki.agent.text_visuals import has_character_diagram
from evowiki.agent.tools import ToolContext, ToolResult
from evowiki.assistant.tools.actions import ActionDeps, build_action_tools
from evowiki.bot.actions import ActionRefused
from evowiki.bot.diffs import Names
from evowiki.bot.revise_schemas import revise_out
from evowiki.bot.section_visual import VisualJob, compose_body
from evowiki.deep.contracts import InteractiveDraft, Probe, RenderCheck
from evowiki.deep.store import MemoryAssetStore
from evowiki.wikiapp.organize import Writing
from evowiki.wikiapp.refine_spec import WikiSpecSource
from evowiki.wikiapp.revise import RevisionDraft
from tests.bot.test_executors_revise import NOW, Env, new_id
from tests.bot.test_practice_flow import dbos_env  # noqa: F401 — shared durable writer fixture
from tests.service.conftest import services as service_fixture  # noqa: F401 — shared HTTP services
from tests.wikiapp.test_revise import refine, sec


class Writer:
    def __init__(self, extra_section=False, mutate=None):
        self.inputs = []
        self.extra_section = extra_section
        self.mutate = mutate

    def write(self, inp, *, budget):
        self.inputs.append((inp, budget))
        if self.mutate:
            self.mutate()
        visual = InteractiveDraft(interactive_id="mean-picture", section_id="s2", title="均值示意图",
                                  learning_question="均值怎样平衡？", html="<p>看三个数的平衡</p>",
                                  height=400, probes=(Probe(target="#b", action="click", expect_change="#out"),))
        sections = [sec("s2", "旧的一句。\n旧的两句。\n\n{{interactive:mean-picture}}")]
        if self.extra_section:
            sections.append(sec("s3", "不该改"))
        draft = refine("pg1", *sections).model_copy(update={"interactives": (visual,)})
        check = RenderCheck(interactive_id="mean-picture", ok=True)
        return SimpleNamespace(model="selected-model", outcome=SimpleNamespace(
            draft=draft, checks={"mean-picture": check}))


@pytest.fixture
def env(pool):
    return Env(pool, MemoryAssetStore())


def compose(env, writer=None, sid="s2"):
    writer = writer or Writer()
    kit = Writing(writer=writer, specs=WikiSpecSource(env.repo), assets=env.ex._assets)
    job = VisualJob(user_id="u1", page_id="pg1", section_id=sid, request="这一节配图", now=NOW, key="test")
    return compose_body(job, env.repo, kit, new_id), writer


def test_the_visual_is_previewable_then_only_its_section_changes_and_undo_restores_everything(env):
    before = env.rec()
    data, writer = compose(env)
    draft = RevisionDraft.model_validate(data)
    assert env.rec() == before
    assert draft.scope == ("s2",) and "embedded-html" in draft.preview["s2"]
    inp, budget = writer.inputs[0]
    assert inp.scope == inp.required == ("s2",)
    assert budget.limit_usd == env.repo.load("u1").state.refine.per_page_limit_usd
    row = env.propose(draft=draft)
    names = Names(env.repo.load("u1").state)
    preview = revise_out(row, names, {})
    assert [s.section_id for s in preview.sections] == ["s2"]
    assert "embedded-html" in preview.sections[0].new_body and preview.unchanged == 2
    applied = env.ledger.approve("u1", row.action_id)
    assert applied.status == "executed"
    after = env.rec()
    for sid in ("s1", "s3"):
        assert after.page.texts[sid] == before.page.texts[sid]
    assert after.page.outline.sections == before.page.outline.sections
    assert "embedded-html" in after.page.texts["s2"].body
    assert env.ledger.undo("u1", row.action_id).status == "undone"
    assert env.rec().page == before.page


def test_unknown_or_check_section_is_rejected_before_the_writer_runs(env):
    writer = Writer()
    for sid in ("missing", "q1"):
        with pytest.raises(ActionRefused):
            compose(env, writer, sid)
    assert writer.inputs == []


def test_the_revision_asset_is_readable_over_http_before_approval(service_fixture, env):  # noqa: F811
    import re

    from fastapi.testclient import TestClient

    from evowiki.service.app import create_app
    data, _ = compose(env)
    draft = RevisionDraft.model_validate(data)
    row = env.propose(draft=draft)
    service_fixture.deep = SimpleNamespace(assets=env.ex._assets)
    client = TestClient(create_app(service_fixture))
    asset_id = re.search(r"src: assets/([^\s.]+)\.html", draft.preview["s2"])[1]
    headers = {"X-EvoWiki-User": "u1"}
    meta = client.get(f"/wiki/assets/{asset_id}/meta", headers=headers)
    picture = client.get(f"/wiki/assets/{asset_id}", headers=headers)
    assert meta.status_code == picture.status_code == 200
    assert "看三个数的平衡" in picture.text
    assert env.ledger.get("u1", row.action_id).status == "proposed"
    assert "embedded-html" not in env.rec().page.texts["s2"].body


def test_the_real_page_writer_searches_places_and_previews_a_picture_without_changing_the_page(env):
    from evowiki.deep.writer import PageWriter
    from tests.deep.fakes import DeepScript, FakeChecker, call, finish, library_empty, section, turn
    from tests.deep.test_images import FakeImages
    images = FakeImages()
    script = DeepScript().plan().write(
        turn(call("find_image", query="number line balance", purpose="看清均值")),
        turn(section("s2", body="旧的一句。\n旧的两句。\n\n{{image:mean-image}}"),
             call("place_image", candidate="img1", image_id="mean-image", section_id="s2", alt="均值示意图")),
        turn(finish()))
    kit = Writing(writer=PageWriter(llm=script.llm(), checker=FakeChecker(),
                                    library=library_empty(), images=images),
                  specs=WikiSpecSource(env.repo), assets=env.ex._assets)
    job = VisualJob(user_id="u1", page_id="pg1", section_id="s2", request="这一节配图", now=NOW, key="real-writer")
    before = env.rec()
    draft = compose_body(job, env.repo, kit, new_id)
    assert images.searched == ["number line balance"]
    assert len(images.fetched) == 1 and "evowiki-image" in draft["preview"]["s2"]
    assert env.rec() == before


def test_a_spent_budget_refuses_before_any_model_call_and_writes_no_draft(env):
    from evowiki.deep.writer import PageWriter
    from evowiki.llm.usage import BudgetExceededError
    from tests.deep.fakes import DeepScript, FakeChecker, library_empty
    class Spent:
        def check(self, account):
            raise BudgetExceededError(account, "day", 1.0)
        def record(self, *args, **kwargs):
            raise AssertionError("nothing may be charged")
    script = DeepScript()
    kit = Writing(writer=PageWriter(llm=script.llm(Spent()), checker=FakeChecker(), library=library_empty()),
                  specs=WikiSpecSource(env.repo), assets=env.ex._assets)
    job = VisualJob(user_id="u1", page_id="pg1", section_id="s2", request="这一节配图", now=NOW, key="no-budget")
    before = env.repo.load("u1")
    with pytest.raises(ActionRefused):
        compose_body(job, env.repo, kit, new_id)
    assert script.served == [] and env.repo.load("u1").wiki_seq == before.wiki_seq


def test_the_writer_cannot_change_other_sections(env):
    before = env.rec()
    with pytest.raises(ActionRefused, match="其他段落"):
        compose(env, Writer(extra_section=True))
    assert env.rec() == before


def test_replaying_the_durable_visual_job_does_not_repeat_model_or_image_calls(dbos_env):  # noqa: F811
    from evowiki.bot.section_visual import compose
    from tests.deep.fakes import call, finish, section, turn
    from tests.deep.test_images import FakeImages
    repo, ids, script, _, kit, pid = dbos_env
    images = FakeImages()
    kit.writer.images = images
    script.plan().write(
        turn(call("find_image", query="number line balance", purpose="看清均值")),
        turn(section("s2", body="看这张图。\n\n{{image:mean-image}}"),
             call("place_image", candidate="img1", image_id="mean-image", section_id="s2", alt="均值示意图")),
        turn(finish()))
    job = VisualJob(user_id="u1", page_id=pid, section_id="s2", request="这一节配图", now=NOW, key="durable-visual")
    first = compose(job, repo=repo, writing=kit, new_id=ids, on_dbos=True)
    calls = len(script.served)
    again = compose(job, repo=repo, writing=kit, new_id=ids, on_dbos=True)
    assert again == first and len(script.served) == calls
    assert images.searched == ["number line balance"] and len(images.fetched) == 1
    bad = job.model_copy(update={"section_id": "missing", "key": "missing-visual"})
    with pytest.raises(ActionRefused, match="没有这一节"):
        compose(bad, repo=repo, writing=kit, new_id=ids, on_dbos=True)
    assert len(script.served) == calls


def test_a_stale_draft_is_not_proposed(env):
    def change():
        rec = env.rec()
        text = rec.page.texts["s1"].model_copy(update={"body": "刚改过正文"})
        page = rec.page.model_copy(update={"texts": {**rec.page.texts, "s1": text}})
        env.save(rec.model_copy(update={"page": page}))
    with pytest.raises(ActionRefused, match="被改过"):
        compose(env, Writer(mutate=change))


def test_retrying_a_visual_draft_keeps_the_illustration_request_instead_of_revising_from_materials(env, monkeypatch):
    from evowiki.service import deps
    from evowiki.wikiapp.page_version import page_fingerprint
    data, _ = compose(env)
    draft = RevisionDraft.model_validate(data)
    request = {"section_id": "s2", "request": "这一节配图"}
    old = env.propose(draft=draft, illustration=request)
    calls = []
    def redraw(svc, user_id, page_id, sid, asked, key):
        calls.append((user_id, page_id, sid, asked))
        return draft.model_copy(update={"base_version": page_fingerprint(env.rec())})
    monkeypatch.setattr(deps, "assistant_illustrate", redraw)
    svc = SimpleNamespace(actions=env.ledger, pool=env.pool, new_id=new_id, clock=lambda: NOW)
    new = deps.restart_revise(svc, "u1", old)
    assert new.status == "proposed" and new.level == "ask"
    assert new.content["illustration"] == request
    assert calls == [("u1", "pg1", "s2", "这一节配图")]
    assert env.ledger.get("u1", old.action_id).status == "rejected"


def test_the_assistant_tool_always_proposes_a_confirmable_revision_and_replay_does_not_write_again(env):
    data, writer = compose(env)
    calls = []
    def illustrate(*args):
        calls.append(args)
        return RevisionDraft.model_validate(data)
    deps = ActionDeps(repo=env.repo, ledger=env.ledger, clock=lambda: NOW, new_id=new_id,
                      illustrate=illustrate)
    tool = next(t for t in build_action_tools(deps) if t.spec.name == "illustrate_section")
    context = ToolContext(user_id="u1", session_id="s1", page_id="pg1")
    args = {"section_id": "s2", "request": "这一节配图"}
    from evowiki.agent import durable
    with durable.virtual_workflow("visual-tool-test"):
        result = ToolResult.model_validate(durable.step(
            "tool", lambda: tool.run(args, context).model_dump(mode="json")))
    assert result.error is None, result.content
    assert result.action.confirm and result.action.kind == "page_revise"
    row = env.ledger.get("u1", result.action.action_id)
    assert env.ledger.level("u1", "page_body") == "auto_undo"
    assert row.level == "ask" and row.status == "proposed"
    assert len(calls) == 1
    # An outstanding draft blocks a second paid composition.
    refused = tool.run(args, context)
    assert refused.error and len(calls) == 1
    unknown = tool.run({**args, "section_id": "missing"}, context)
    assert unknown.error and len(calls) == 1
    assert len(writer.inputs) == 1


@pytest.mark.parametrize("text", ["┌────┐\n│ 数 │\n└────┘", "+-----+\n| box |\n+-----+"])
def test_obvious_character_boxes_are_diagrams(text):
    assert has_character_diagram(text)


@pytest.mark.parametrize("text", ["| x | y |\n| --- | --- |\n| 1 | 2 |", "$a+b=c$",
                                  "```python\ns = '┌──┐'\nprint(s)\n```", "x → y 是一个映射"])
def test_tables_math_code_and_prose_are_not_character_diagrams(text):
    assert not has_character_diagram(text)
