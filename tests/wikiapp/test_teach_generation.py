"""R1 Task 4: the teaching spec in page generation (applicable items + params lines in every
writer prompt, the page domain from the embedding) and the per-section SectionLog written in the same commit
as the page it explains (organize → organizer, deeper/other → writer, lazy patch → patch)."""

from dataclasses import replace

import pytest

from evowiki.deep.agent import write_prompt
from evowiki.deep.tools import Workspace
from evowiki.goals.contracts import Band
from evowiki.retrieval.embed import HashingEmbedder
from evowiki.teach.contracts import RepParams
from evowiki.teach.default_spec import DEFAULT_ITEMS
from evowiki.teach.render import param_lines
from evowiki.wikiapp.events import (
    PageSaved,
    SectionLogsSaved,
    WikiEvent,
)
from evowiki.wikiapp.kwrite import page_spec
from evowiki.wikiapp.repo import Commit, MemoryRepo
from evowiki.wikiapp.sectionlog import WHY_MAX, log_versions, section_logs
from evowiki.wikiapp.teach import add_item_flow, edit_item_flow, set_params_flow
from evowiki.wikiapp.teachspec import (
    applicable,
    effective_spec,
    page_teach,
)
from evowiki.wikiapp.wiki_schemas import SpecItemIn, SpecItemPatchIn
from tests.wikiapp.fake_writer import WRITE, writing
from tests.wikiapp.script import Script
from tests.wikiapp.test_goal import NOW, Ids
from tests.wikiapp.test_kchange import rewrite
from tests.wikiapp.test_organize import page1, run, script_page, text
from tests.wikiapp.test_page import LINE, declare, open_, organized

CONCEPT_IDS = tuple(i.item_id for i in DEFAULT_ITEMS if i.scope in ("any", "concept", "exercise"))
QUICK_IDS = tuple(i.item_id for i in DEFAULT_ITEMS if i.scope in ("any", "quick"))


class SpyRepo(MemoryRepo):
    """MemoryRepo that keeps every Commit, to check what went into one transaction."""

    def __init__(self, users):
        super().__init__(users)
        self.commits: list[Commit] = []

    def commit(self, snap, c, *, once=None):
        out = super().commit(snap, c, once=once)
        self.commits.append(c)
        return out


@pytest.fixture
def env():
    return SpyRepo(["u1"]), Script(), Ids()


def first_message(inp) -> str:
    """The page writer's first message for `inp` (before plan_page: the outline part included)."""
    return write_prompt(inp, (), Workspace(inp.page))


def embedding(env):
    """The writer kit with an embedder: the page's domain is the nearest spec domain label."""
    return replace(writing(env), embedder=HashingEmbedder())


def st(env):
    return env[0].load("u1").state


def kw(env):
    return {"now": NOW, "new_id": env[2]}


def page_commit(env, action: str) -> tuple[PageSaved, SectionLogsSaved | None]:
    """The PageSaved of the last commit that saved the page with `action`, and the
    SectionLogsSaved of that same commit (None if it has none). Asserts the logs come right
    after the page."""
    for c in reversed(env[0].commits):
        bodies = [e.body for e in c.wiki]
        for i, b in enumerate(bodies):
            if isinstance(b, PageSaved) and b.action == action:
                logs = [x for x in bodies if isinstance(x, SectionLogsSaved)]
                assert len(logs) <= 1
                if logs:
                    assert bodies[i + 1] is logs[0]
                return b, (logs[0] if logs else None)
    raise AssertionError(f"no commit saved the page with action {action}")


def collapse(env, pid):
    repo, _, ids = env
    snap = repo.load("u1")
    rec = snap.state.pages[pid].model_copy(update={"shelved": True})
    repo.commit(snap, Commit(wiki=[WikiEvent(event_id=ids("wev"), ts=NOW,
                                             body=PageSaved(record=rec, action="shelved"))]))


# ---- organize ----

def test_organize_logs_every_section_in_the_page_commit(env):
    pid = organized(env)
    saved, logs = page_commit(env, "organized")
    assert logs is not None
    assert [g.section_id for g in logs.logs] == ["s1", "s2", "s3", "s4"]     # checks included
    by = {g.section_id: g for g in logs.logs}
    for g in logs.logs:                     # written sections: the writer; checks: the organizer
        producer = "organizer" if g.section_id == "s4" else "writer"
        assert (g.page_id, g.producer, g.at) == (pid, producer, NOW)
        assert g.spec_items == CONCEPT_IDS and g.spec_version == 1
        assert g.infer_version == 1 and g.guardrails_ok      # no track-A version stored
    s2 = by["s2"]
    assert s2.cells == ("c1.procedure", "c1.interpret") and s2.role == "explain"
    assert s2.bands == {"c1.procedure": Band.SHAKY.value, "c1.interpret": Band.NONE.value}
    assert s2.why == "目标格，计算模糊"
    assert by["s4"].role == "check" and by["s4"].cells == ("c1.interpret",)
    assert by["s1"].bands == {} and by["s1"].cells == ()
    state = st(env)
    assert state.section_log(pid, "s2") == s2
    assert log_versions(state, pid, "s2") == (1, 1) and log_versions(state, "nope") == (None, None)
    assert saved.record.domain is None


def test_logs_carry_the_users_current_track_a_version(env):
    env[0]._infer_version_for = lambda user_id: 3        # R2 registry: θ_infer,u at v3
    pid = organized(env)
    _, logs = page_commit(env, "organized")
    assert logs is not None and {g.infer_version for g in logs.logs} == {3}
    assert log_versions(st(env), pid, "s2") == (1, 3)


def test_reorganize_logs_again_with_the_current_spec_version(env):
    repo, s, _ = env
    pid = organized(env)
    add_item_flow(repo, "u1", SpecItemIn(text="每段先给结论"), **kw(env))     # v1 + v2
    script_page(s)          # new guidelines: no cache hit
    run(env, pid)
    _, logs = page_commit(env, "reorganized")
    assert logs is not None and {g.spec_version for g in logs.logs} == {2}
    new_id = st(env).spec.items[-1].item_id
    assert all(g.spec_items == (*CONCEPT_IDS, new_id) for g in logs.logs)


def test_section_logs_why_cap_and_guardrails_recheck(env):
    pid = organized(env)
    snap = env[0].load("u1")
    outline = snap.state.pages[pid].page.outline
    long_why = outline.model_copy(update={"sections": tuple(
        x.model_copy(update={"why": "长" * 700}) if x.id == "s1" else x
        for x in outline.sections)})
    teach = page_teach(snap.state, pid)
    spec = page_spec(snap.state, snap.k, pid, teach=teach)
    logs = section_logs(long_why, ["s1"], producer="organizer", spec=spec, k=snap.kview(NOW),
                        teach=teach, at=NOW, infer_version=snap.infer_version)
    assert [g.section_id for g in logs] == ["s1"] and len(logs[0].why) == WHY_MAX
    declare(env, "know", "c1.procedure")      # s2 now explains a known cell: rule ③ names s2
    snap = env[0].load("u1")
    logs = section_logs(outline, None, producer="organizer", spec=spec, k=snap.kview(NOW),
                        teach=teach, at=NOW, infer_version=snap.infer_version)
    assert {g.section_id: g.guardrails_ok for g in logs} == {
        "s1": True, "s2": False, "s3": True, "s4": True}


# ---- prompts ----

def test_the_writer_prompt_carries_items_and_params_lines(env):
    from evowiki.deep.agent import params_text
    repo, s, _ = env
    pid = page1(env)
    script_page(s)
    kit = writing(env)
    run(env, pid, writing=kit)
    inp = kit.writer.inputs[0]                     # the page writer's input and first message
    prompt = first_message(inp)
    assert DEFAULT_ITEMS[0].text in prompt         # the outline part's principles and the spec
    assert DEFAULT_ITEMS[0].text in [i.text for i in inp.teaching.items]
    assert all(line in prompt for line in param_lines(RepParams()))
    quick_only = next(i for i in DEFAULT_ITEMS if i.scope == "quick")
    assert quick_only.text not in prompt
    set_params_flow(repo, "u1", RepParams(recap_max_per_page=0, analogy_level=2), **kw(env))
    script_page(s)
    run(env, pid, writing=kit)
    text = params_text(kit.writer.inputs[-1])
    assert "这一页不放提醒卡" in text and "可以多用类比，但要说明类比在哪里不成立" in text
    assert s.pending() == {}


def test_quick_page_gets_quick_items_not_concept_ones(env):
    repo, s, _ = env
    pid = organized(env)
    collapse(env, pid)
    state = st(env)
    teach = page_teach(state, pid)
    assert teach.item_ids == QUICK_IDS
    assert page_spec(state, repo.load("u1").k, pid).guidelines == teach.guidelines
    script_page(s)
    kit = writing(env)
    run(env, pid, writing=kit)
    _, logs = page_commit(env, "reorganized")
    assert logs is not None and all(g.spec_items == QUICK_IDS for g in logs.logs)
    concept_only = next(i for i in DEFAULT_ITEMS if i.scope == "concept")
    prompt = first_message(kit.writer.inputs[-1])
    assert concept_only.text not in prompt
    assert next(i for i in DEFAULT_ITEMS if i.scope == "quick").text in prompt


# ---- domain ----

def domain_item(env, domain="统计"):
    add_item_flow(env[0], "u1", SpecItemIn(text="统计页先画分布", domain=domain), **kw(env))
    return st(env).spec.items[-1].item_id


def test_no_domain_without_domain_items_or_an_embedder(env):
    _, s, _ = env
    pid = page1(env)
    script_page(s)
    run(env, pid, writing=embedding(env))
    assert st(env).pages[pid].domain is None                    # no domain items
    domain_item(env, "z 分数 统计")
    script_page(s)
    run(env, pid)                                               # no embedder: none, no call
    assert st(env).pages[pid].domain is None and s.pending() == {}


def test_domain_items_apply_once_the_embedding_picks_the_domain(env):
    _, s, _ = env
    pid = page1(env)
    did = domain_item(env, "z 分数 统计")
    other = domain_item(env, "烹饪 菜谱")
    # page domain still None: both reach it, each with its domain named; the pick drops the other
    assert {did, other} <= set(page_teach(st(env), pid).item_ids)
    script_page(s)
    kit = embedding(env)
    items, gw = run(env, pid, writing=kit)
    assert items[-1][0] == "done" and gw.calls == 0              # no model call for the domain
    saved, logs = page_commit(env, "organized")
    assert saved.record.domain == "z 分数 统计"
    assert logs is not None and all(g.spec_items == (*CONCEPT_IDS, did) for g in logs.logs)
    assert "统计页先画分布" in first_message(kit.writer.inputs[0])
    assert items[-1][1].llm_calls == 3                          # the writer's 3 writes


def test_a_weak_nearest_label_or_a_failing_embedder_leaves_the_domain_none_and_the_writer_judges(
        env):
    class Broken(HashingEmbedder):
        def embed_query(self, text):
            raise RuntimeError("down")

    _, s, _ = env
    pid = page1(env)
    did = domain_item(env, "烹饪 菜谱")
    domain_item(env, "法律 合同")
    script_page(s)
    kit = embedding(env)
    items, _ = run(env, pid, writing=kit)
    assert items[-1][0] == "done" and st(env).pages[pid].domain is None
    _, logs = page_commit(env, "organized")
    assert logs is not None and all(did in g.spec_items for g in logs.logs)
    assert "统计页先画分布（只用于「烹饪 菜谱」的内容）" in first_message(kit.writer.inputs[0])
    script_page(s)
    items, _ = run(env, pid, writing=replace(writing(env), embedder=Broken()))
    assert items[-1][0] == "done" and st(env).pages[pid].domain is None


def test_applicable_domain_match_is_casefold(env):
    page1(env)
    domain_item(env, "Statistics")
    spec = effective_spec(st(env))
    assert [i.text for i in applicable(spec, "concept", " statistics ")][-1] == "统计页先画分布"


# ---- writer / patch ----

def test_other_rewrite_logs_writer_for_that_section(env):
    _, s, _ = env
    pid = organized(env)
    s.push(WRITE, text("s2", "换个角度讲 z"))
    items, _ = rewrite(env, pid, "s2", "other")
    assert items[-1][0] == "done"
    _, logs = page_commit(env, "feedback")
    assert logs is not None and [(g.section_id, g.producer) for g in logs.logs] == [
        ("s2", "writer")]
    g = logs.logs[0]
    assert g.spec_items == CONCEPT_IDS and g.spec_version == 1 and g.role == "explain"
    assert st(env).section_log(pid, "s2") == g
    assert st(env).section_log(pid, "s1").producer == "writer"      # the page write


def test_deeper_with_a_conflict_logs_writer_and_patch_in_one_event(env):
    _, s, _ = env
    pid = organized(env, LINE)
    declare(env, "dont", "c2.term")
    s.push(WRITE, text("s2", "z 讲深一点"))
    items, _ = rewrite(env, pid, "s2", "deeper")
    assert items[-1][0] == "done"
    _, logs = page_commit(env, "feedback")
    assert logs is not None and [(g.section_id, g.producer) for g in logs.logs] == [("s2", "writer")]


def test_open_without_patch_writes_no_logs(env):
    pid = organized(env)
    declare(env, "know", "c2.term")                       # folds only, no conflict, 0 calls
    open_(env, pid)
    _, logs = page_commit(env, "opened")
    assert logs is None


def test_renamed_domain_is_picked_again_on_reorganize(env):
    """A stored domain no spec item names any more (统计 → 统计学) is picked again on reorganize."""
    repo, s, _ = env
    pid = page1(env)
    did = domain_item(env, "z 分数 统计")
    script_page(s)
    run(env, pid, writing=embedding(env))
    assert st(env).pages[pid].domain == "z 分数 统计"
    edit_item_flow(repo, "u1", did, SpecItemPatchIn(domain="z 分数 统计学"), **kw(env))
    assert did not in page_teach(st(env), pid).item_ids        # stale: matches nothing
    script_page(s)
    kit = embedding(env)
    run(env, pid, writing=kit)
    assert st(env).pages[pid].domain == "z 分数 统计学"
    _, logs = page_commit(env, "reorganized")
    assert logs is not None and all(did in g.spec_items for g in logs.logs)
    assert "统计页先画分布" in first_message(kit.writer.inputs[-1])
    assert s.pending() == {}


def test_stored_domain_is_kept_when_no_domain_items_remain(env):
    repo, s, _ = env
    pid = page1(env)
    did = domain_item(env, "z 分数 统计")
    script_page(s)
    run(env, pid, writing=embedding(env))
    edit_item_flow(repo, "u1", did, SpecItemPatchIn(domain=None), **kw(env))
    script_page(s)
    run(env, pid)
    assert st(env).pages[pid].domain == "z 分数 统计"
    assert s.pending() == {}


def test_stored_domain_still_listed_is_not_picked_again_casefold(env):
    repo, s, _ = env
    pid = page1(env)
    did = domain_item(env, "z 分数 Statistics")
    script_page(s)
    run(env, pid, writing=embedding(env))
    assert st(env).pages[pid].domain == "z 分数 Statistics"
    edit_item_flow(repo, "u1", did, SpecItemPatchIn(domain=" Z 分数 STATISTICS "), **kw(env))
    script_page(s)
    run(env, pid)                                   # no embedder: the stored one is still listed
    assert st(env).pages[pid].domain == "z 分数 Statistics"
