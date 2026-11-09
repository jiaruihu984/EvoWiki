"""`page_revise` (materials phase 6 §7 D): a whole-page draft from materials, confirmed through the
action ledger as a whole (no draft edits), swapped in one commit with the ledger's effect, undoable.
Real Postgres; no LLM (drafts are built by hand)."""

from datetime import UTC, datetime

import pytest

from evowiki.bot.actions import STALE_DETAIL, ActionLedger, NotDecidable, can_undo
from evowiki.bot.executors_revise import PageReviseExecutor
from evowiki.deep.contracts import InteractiveDraft, Probe, RenderCheck
from evowiki.deep.store import MemoryAssetStore
from evowiki.sources.cite import CiteIssue
from evowiki.wikiapp.events import PageRecord, PageSaved, PageSource, WikiEvent
from evowiki.wikiapp.page_version import page_fingerprint
from evowiki.wikiapp.repo import Commit, PgRepo
from evowiki.wikiapp.revise import KIND, RevisionDraft, RevisionSource
from tests.runtime.helpers import make_user
from tests.wikiapp.partial_write_check import assert_partial_write
from tests.wikiapp.test_revise import page_record, refine, sec

NOW = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)
_n = iter(range(100_000))


def new_id(prefix):
    return f"{prefix}_{next(_n)}"


class RecordingRepo(PgRepo):
    """PgRepo that keeps every Commit it was given."""

    def __init__(self, pool):
        super().__init__(pool)
        self.commits: list[Commit] = []

    def commit(self, snap, c, *, once=None):
        self.commits.append(c)
        return super().commit(snap, c, once=once)


class Env:
    def __init__(self, pool, assets=None):
        make_user(pool, "u1")
        self.pool, self.repo = pool, RecordingRepo(pool)
        self.save(page_record())
        self.ex = PageReviseExecutor(self.repo, assets, lambda: NOW, new_id)
        self.ledger = ActionLedger(pool, lambda: NOW, new_id, {KIND: self.ex})

    def rec(self, page_id="pg1") -> PageRecord:
        return self.repo.load("u1").state.pages[page_id]

    def save(self, rec: PageRecord) -> None:
        self.repo.commit(self.repo.load("u1"), Commit(wiki=[WikiEvent(
            event_id=new_id("wev"), ts=NOW, body=PageSaved(record=rec, action="test"))]))

    def draft(self, *sections, **kw) -> RevisionDraft:
        rec = self.rec()
        sections = sections or (sec("s1", "新开头", heading="开头"),
                                sec("s2", "新的均值", heading="均值怎么算"))
        return RevisionDraft(page_id="pg1", base_version=page_fingerprint(rec),
                             draft=refine("pg1", *sections), **kw)

    def propose(self, state="ready", draft=None, key="revise:run_1", page_id="pg1", **extra):
        rec = self.rec(page_id)
        if state == "ready" and draft is None:
            draft = self.draft()
        content = {"state": state, "confirm": True, "page_id": page_id, "source_ids": ["src_1"],
                   "run_id": "run_1", "detail": "",
                   "draft": None if draft is None else draft.model_dump(mode="json"), **extra}
        return self.ledger.propose(
            "u1", kind=KIND, scope="page_body", source="user", summary=f"按资料改「{rec.title}」",
            why="用了讲义", content=content, key=key, page_id=page_id,
            page_version=page_fingerprint(rec), level="ask")


@pytest.fixture
def env(pool):
    return Env(pool)


def test_a_draft_still_being_written_cannot_be_approved(env):
    row = env.propose(state="writing")
    with pytest.raises(NotDecidable, match="草稿还在写"):
        env.ledger.approve("u1", row.action_id)
    assert env.ledger.get("u1", row.action_id).status == "proposed"     # nothing moved
    with pytest.raises(NotDecidable, match="不能带修改后的草稿"):
        env.ledger.approve("u1", row.action_id, draft={"choices": {"c1": "page"}})
    assert env.ledger.get("u1", row.action_id).status == "proposed"


def test_a_failed_draft_fails_the_row(env):
    row = env.propose(state="failed")
    out = env.ledger.approve("u1", row.action_id)
    assert (out.status, out.detail) == ("failed", "这条草稿没写成")
    assert env.rec().page == page_record().page


def test_approving_a_ready_draft_swaps_the_page_in_the_effects_commit(env):
    before = env.rec()
    row = env.propose()
    n = len(env.repo.commits)
    out = env.ledger.approve("u1", row.action_id)
    assert out.status == "executed"
    [c] = env.repo.commits[n:]                        # one commit: page, effect, undo together
    assert [e.body.action for e in c.wiki if isinstance(e.body, PageSaved)] == ["source_revise"]
    assert any(f.__qualname__.endswith("approve.<locals>.effect") for f in c.extra)
    page = env.rec().page
    assert [s.id for s in page.outline.sections] == ["s1", "s2", "q1"]   # s3 is gone
    assert page.texts["s1"].body == "新开头"
    assert env.rec().revisions == 1
    assert_partial_write(env.repo, "pg1", "rewrite", {"s1", "s2"}, producer="writer")
    assert out.undo["prev_record"] == before.model_dump(mode="json")
    assert out.undo["after_version"] == page_fingerprint(env.rec())
    assert can_undo(out)
    assert set(out.result["wiki_event_ids"]) == {e.event_id for e in c.wiki}


def test_a_page_changed_since_the_draft_is_not_overwritten(env):
    row = env.propose()
    changed = env.rec().page.model_copy(update={"texts": {
        **env.rec().page.texts, "s3": env.rec().page.texts["s3"].model_copy(
            update={"body": "别的流程改过"})}})
    env.save(env.rec().model_copy(update={"page": changed}))
    out = env.ledger.approve("u1", row.action_id)
    assert (out.status, out.detail) == ("failed", STALE_DETAIL)
    assert env.rec().page.texts["s3"].body == "别的流程改过"
    assert env.rec().revisions == 0


def test_undo_restores_the_old_page_and_refuses_after_a_later_change(env):
    before = env.rec()
    issue = CiteIssue(kind="quote_not_found", mark="〔讲义 p.3：「没有的话」〕", source_id="src_1",
                      page=3, quote="没有的话", section_id="s1")
    row = env.propose(draft=env.draft(sources=(RevisionSource(source_id="src_1", title="讲义"),),
                                      cite_issues=(issue,)))
    env.ledger.approve("u1", row.action_id)
    assert env.rec().sources and env.rec().cite_issues == (issue,)
    out = env.ledger.undo("u1", row.action_id)
    assert out.status == "undone"
    assert env.rec().page == before.page and env.rec().revisions == 0
    assert (env.rec().sources, env.rec().cite_issues) == (before.sources, before.cite_issues)

    row2 = env.propose(key="revise:run_2")
    env.ledger.approve("u1", row2.action_id)
    later = env.rec().page.model_copy(update={"texts": {
        **env.rec().page.texts, "s2": env.rec().page.texts["s2"].model_copy(
            update={"body": "之后又改"})}})
    env.save(env.rec().model_copy(update={"page": later}))
    with pytest.raises(NotDecidable, match="这一页之后又改过了，不能撤销"):
        env.ledger.undo("u1", row2.action_id)
    assert env.rec().page.texts["s2"].body == "之后又改"
    assert env.ledger.get("u1", row2.action_id).status == "executed"


def test_the_draft_is_not_editable(env):
    row = env.propose()
    assert not hasattr(env.ex, "revise")
    with pytest.raises(NotDecidable, match="不能带修改后的草稿"):
        env.ledger.approve("u1", row.action_id, draft={"sections": []})
    assert env.ledger.get("u1", row.action_id).status == "proposed"
    assert env.rec().page == page_record().page


def test_a_user_note_is_never_changed(env):
    env.save(PageRecord(page_id="note1", title="我的笔记", goal="", concepts=(),
                        targets=(), created_at=NOW))
    d = RevisionDraft(page_id="note1", base_version="absent",
                      draft=refine("note1", sec("s1", "x")))
    row = env.propose(draft=d, page_id="note1", key="revise:note")
    out = env.ledger.approve("u1", row.action_id)
    assert (out.status, out.detail) == ("failed", "这是你自己的笔记，EvoWiki 不改")
    assert env.rec("note1").page is None


HTML = "<!doctype html><button id=b>加一</button><p id=out>0</p>"


def interactive_draft(env) -> RevisionDraft:
    ia = InteractiveDraft(interactive_id="demo", section_id="s1", title="演示",
                          learning_question="会怎样？", html=HTML, height=480,
                          probes=(Probe(target="#b", action="click", expect_change="#out"),))
    rd = env.draft(sec("s1", "看这个：{{interactive:demo}}", heading="开头"))
    draft = rd.draft.model_copy(update={"interactives": (ia,)})
    return rd.model_copy(update={"draft": draft, "checks": (
        RenderCheck(interactive_id="demo", ok=True),)})


def test_an_interactive_without_an_asset_store_is_refused(env):
    row = env.propose(draft=interactive_draft(env))
    out = env.ledger.approve("u1", row.action_id)
    assert (out.status, out.detail) == ("failed", "没有交互资源库，不能提交带交互的草稿")
    assert env.rec().page == page_record().page


def test_an_interactive_is_stored_in_the_same_commit(pool):
    assets = MemoryAssetStore()
    env = Env(pool, assets)
    row = env.propose(draft=interactive_draft(env))
    out = env.ledger.approve("u1", row.action_id)
    assert out.status == "executed"
    [asset] = assets.rows.values()
    assert asset.meta.interactive_id == "demo" and out.result["assets"] == [asset.meta.asset_id]
    assert f"assets/{asset.meta.asset_id}.html" in env.rec().page.texts["s1"].body


def test_an_executed_revision_records_its_materials_and_citation_issues(env):
    """The page keeps the draft's materials (`PageRecord.sources`, with their titles then) and the
    citation check's issues, like a page written from materials."""
    issue = CiteIssue(kind="quote_not_found", mark="〔讲义 p.3：「不存在的话」〕", source_id="src_1",
                      page=3, quote="不存在的话", section_id="s1")
    d = env.draft(sec("s1", "新开头〔讲义 p.3｜3〕", heading="开头"),
                  sec("s2", "新的均值", heading="均值怎么算"),
                  sources=(RevisionSource(source_id="src_1", title="讲义", section_ids=("x1",),
                                          page_offset=0),),
                  cite_issues=(issue,))
    out = env.ledger.approve("u1", env.propose(draft=d).action_id)
    assert out.status == "executed"
    rec = env.rec()
    assert rec.sources == (PageSource(source_id="src_1", title="讲义", page_offset=0),)
    assert rec.cite_issues == (issue,)
