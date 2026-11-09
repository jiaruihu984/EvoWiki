"""Pages the exam model could not trust are re-read by a model that can see (materials phase 5
F, after the live MathDoc run): candidate pages, Claude's re-read stored and patched into the
sections, only the changed batches extracted again, however many pages (no cost quote). Real
Postgres, fake page readers, a
scripted JsonLLM: no model is ever called."""

from datetime import UTC, datetime

import pytest

from evowiki.agent import durable
from evowiki.llm.providers import ProviderRejectedError
from evowiki.retrieval.embed import HashingEmbedder
from evowiki.sources import exam_store, store
from evowiki.sources.exam import ExamPlan, RawItem
from evowiki.sources.exam_flow import ExamDeps, reread_candidates, run_exam
from evowiki.sources.models import ExamItem, PageRead
from evowiki.sources.vision import PageImageRead
from evowiki.wikiapp.repo import PgRepo
from tests.sources._fakes import FakeLLM
from tests.sources.test_importer import add_source
from tests.sources.test_importer_vision import Reader, go, make_env, make_pdf, sections

NOW = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)
FILL = "字" * 3000          # each page its own batch (EXAM_BATCH_CHARS 6000)


def _local(p: int) -> PageImageRead:
    return PageImageRead(f"{p}. 第{p}题本地读的 {FILL}", False)


def _claude(p: int) -> PageImageRead:
    return PageImageRead(f"{p}. 第{p}题看图读清楚了 {FILL}", False)


def _ids():
    n = iter(range(1, 10_000))
    return lambda prefix: f"{prefix}-{next(n)}"


@pytest.fixture
def env(pool):
    pool, import_deps = make_env(pool)
    add_source(pool, "s1", make_pdf("bbb"))
    go(import_deps(local=Reader("local", answer=_local)), "s1")
    with pool.connection() as conn:
        store.add_use(conn, "u1", "s1", "exam", now=NOW)
    claude = Reader("claude", answer=_claude)
    box = {"llm": None}
    deps = ExamDeps(pool=pool, repo=PgRepo(pool), embedder=HashingEmbedder(),
                    llm_for=lambda _u: box["llm"], clock=lambda: NOW, new_id=_ids(),
                    claude=claude, sleep=lambda _s: None)
    return pool, deps, box, claude, import_deps


def _q(p: int, *, code: str | None = None, read: str = "本地读的") -> ExamPlan:
    if code is None:
        return ExamPlan(items=[RawItem(number=str(p), kind="open", stem=f"第{p}题{read}",
                                       page=p)])
    return ExamPlan(items=[RawItem(number=str(p), kind="open", stem=f"第{p}题{read}", page=p,
                                   status="unreadable", problem_code=code, problem="手写压字")])


def run(deps):
    with durable.virtual_workflow("source_exam:u1:s1:test"):
        return run_exam(deps, "u1", "s1")


def items(pool):
    with pool.connection() as conn:
        return exam_store.items_of_source(conn, "u1", "s1")


def reads(pool):
    with pool.connection() as conn:
        return {(p, r.by) for p, r in store.page_reads(conn, "u1", "s1").items()}


@pytest.mark.parametrize("change", ["rerun", "delete"])
def test_generation_changed_during_exam_vision_cannot_save_old_read(env, change):
    from evowiki.sources.exam_flow import _reread

    pool, deps, _, claude, _ = env

    def changed(page):
        with pool.connection() as conn:
            if change == "rerun":
                store.bump_run(conn, "u1", "s1")
            else:
                store.soft_delete(conn, "u1", "s1", NOW)
        return _claude(page)

    claude.answer = changed
    result = _reread(deps, "u1", "s1", 0, [1])
    assert claude.calls == [1]
    assert result["stop"] == "gone" and result["read"] == []
    assert (1, "claude") not in reads(pool)


def test_exam_patch_carries_its_generation_to_the_actual_source_write(env, monkeypatch):
    from evowiki.sources import exam_flow
    from evowiki.sources.importer import _Deleted

    pool, deps, _, _, _ = env
    before = sections(pool, "s1")
    with pool.connection() as conn:
        store.put_page_reads(conn, "u1", "s1", [PageRead(1, "old exam result", "claude", False)], NOW)
    live = exam_flow._live_row

    def changed(*args):
        row = live(*args)
        with pool.connection() as conn:
            store.bump_run(conn, "u1", "s1")
        return row

    monkeypatch.setattr(exam_flow, "_live_row", changed)
    with pytest.raises(_Deleted):
        exam_flow._patch(deps, "u1", "s1", 0, [1])
    assert sections(pool, "s1") == before


def _item(page, problem, item_id="i"):
    return ExamItem(item_id, "s1", "x", page, 0, "1", None, "open", "题", (), None, None, False,
                    "unreadable", problem)


def test_reread_candidates_codes_and_readers():
    got = [_item(1, "handwriting_over_print: 手写压字"), _item(2, "illegible: 乱码"),
           _item(3, "missing_part: 截断"), _item(4, "needs_figure"), _item(5, "illegible"),
           _item(None, "illegible"), _item(6, "other: 手写")]
    reads = {1: PageRead(1, "x", "local", False), 2: PageRead(2, "x", "claude", False),
             3: PageRead(3, "x", "local", False)}
    # 1: local read; 5: text layer (no read); 2: Claude read it already; 3/4/6: other codes
    assert reread_candidates(got, reads) == [1, 5]


def test_the_sections_are_one_batch_per_page(env):
    pool, *_ = env
    from evowiki.sources.exam import batches
    with pool.connection() as conn:
        bs = batches(store.sections(conn, "u1", "s1"))
    assert [b.pages for b in bs] == [(1,), (2,), (3,)]
    assert reads(pool) == {(1, "local"), (2, "local"), (3, "local")}


def test_an_untrusted_page_is_re_read_and_only_its_batch_extracted_again(env):
    pool, deps, box, claude, _ = env
    box["llm"] = llm = FakeLLM(_q(1), _q(2, code="handwriting_over_print"), _q(3),
                               _q(2, read="看图读清楚了"))
    out = run(deps)
    assert claude.calls == [2]
    assert (2, "claude") in reads(pool)
    text = "\n".join(s.text for s in sections(pool, "s1"))
    assert "第2题看图读清楚了" in text and "第2题本地读的" not in text
    prompts = [p for _r, purpose, p in llm.calls if purpose == "sources.exam"]
    assert len(prompts) == 4 and "第2题看图读清楚了" in prompts[3]      # page 2's batch only
    assert out["state"] == "done" and out["unreadable"] == 0 and out["items"] == 3
    got = items(pool)
    assert [(i.number, i.status) for i in got] == [("1", "ok"), ("2", "ok"), ("3", "ok")]
    assert got[0].item_id == "si-1" and got[2].item_id == "si-3"   # 1 and 3 keep their ids
    assert got[1].item_id not in ("si-1", "si-2", "si-3")
    assert got[1].stem == "第2题看图读清楚了"
    with pool.connection() as conn:                               # the patched page is searchable
        chunks = store.chunks_of(conn, "u1", ["s1"])
    assert any("第2题看图读清楚了" in c.text for c in chunks)


def test_many_untrusted_pages_are_all_re_read_without_a_quote(env):
    """No cost gate: however many pages the model could not trust, Claude reads them."""
    pool, deps, box, claude, _ = env
    with pool.connection() as conn:
        store.put_page_reads(conn, "u1", "s1", [PageRead(3, "Claude 读过", "claude", False)],
                             NOW)
    box["llm"] = FakeLLM(_q(1, code="illegible"), _q(2, code="handwriting_over_print"), _q(3),
                         _q(1, read="看图读清楚了"), _q(2, read="看图读清楚了"))
    run(deps)
    assert claude.calls == [1, 2]
    assert {(1, "claude"), (2, "claude")} <= reads(pool)


@pytest.mark.parametrize("claude", [
    Reader("claude", available=False),
    Reader("claude", answer=lambda p: (_ for _ in ()).throw(ProviderRejectedError("402"))),
    Reader("claude", answer=lambda p: PageImageRead(None, False, "empty")),
])
def test_no_reader_a_402_or_an_empty_read_keep_the_items(env, claude):
    pool, deps, box, _c, _ = env
    deps.claude = claude
    box["llm"] = llm = FakeLLM(_q(1), _q(2, code="handwriting_over_print"), _q(3))
    out = run(deps)
    assert len(llm.calls) == 3                                      # no re-extraction
    assert out["state"] == "done" and out["unreadable"] == 1
    assert [i.status for i in items(pool)] == ["ok", "unreadable", "ok"]
    assert (2, "claude") not in reads(pool)
    assert "第2题本地读的" in "\n".join(s.text for s in sections(pool, "s1"))


def test_a_page_claude_read_already_is_never_re_read(env):
    pool, deps, box, claude, _ = env
    with pool.connection() as conn:
        store.put_page_reads(conn, "u1", "s1", [PageRead(2, "Claude 早就读过", "claude", False)],
                             NOW)
    box["llm"] = llm = FakeLLM(_q(1), _q(2, code="illegible"), _q(3))
    out = run(deps)
    assert claude.calls == [] and len(llm.calls) == 3 and out["unreadable"] == 1


def test_a_failed_re_extraction_keeps_the_first_items(env):
    pool, deps, box, claude, _ = env
    box["llm"] = FakeLLM(_q(1), _q(2, code="illegible"), _q(3),
                         ProviderRejectedError("402 balance"))
    out = run(deps)
    assert claude.calls == [2] and out["state"] == "done" and out["unreadable"] == 1
    assert [i.item_id for i in items(pool)] == ["si-1", "si-2", "si-3"]


def test_a_re_extraction_call_that_fails_keeps_that_batchs_items_and_ids(env):
    """Review I1: page 2 holds an unreadable and an ok item; the call for page 2's changed
    batch fails, so the first extraction of page 2 is kept (4 items, same ids)."""
    from evowiki.llm.gateway import LLMParseError
    pool, deps, box, claude, _ = env
    two = ExamPlan(items=[*_q(2, code="handwriting_over_print").items,
                          RawItem(number="2b", kind="open", stem="第2题本地读的另一题", page=2)])
    box["llm"] = FakeLLM(_q(1), two, _q(3), LLMParseError("not json"))
    out = run(deps)
    assert claude.calls == [2]
    assert out["state"] == "done" and out["items"] == 4
    got = items(pool)
    assert [i.item_id for i in got] == ["si-1", "si-2", "si-3", "si-4"]
    assert [i.number for i in got] == ["1", "2", "2b", "3"]


class _Printed(Reader):
    """A fake ClaudePages: `printed_only()` hands out the printed variant, which is recorded."""

    def __init__(self):
        super().__init__("claude", answer=_claude)
        self.variant: Reader | None = None

    def printed_only(self):
        self.variant = Reader("claude", answer=_claude)
        return self.variant


def test_the_exam_re_read_uses_the_printed_only_reader(env):
    _pool, deps, box, _reader, _ = env
    deps.claude = plain = _Printed()
    box["llm"] = FakeLLM(_q(1), _q(2, code="illegible"), _q(3), _q(2, read="看图读清楚了"))
    run(deps)
    assert plain.calls == [] and plain.variant is not None and plain.variant.calls == [2]
