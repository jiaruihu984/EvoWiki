"""The import workflow body (`sources.importer.execute`) through `InlineSourceRuns`: real
Postgres, HashingEmbedder, a fake JsonLLM. No model is ever called."""

from datetime import UTC, datetime
from itertools import count
from pathlib import Path

import pytest

from evowiki.llm.providers import ProviderRejectedError
from evowiki.llm.usage import Account, BudgetExceededError
from evowiki.retrieval.embed import HashingEmbedder
from evowiki.sources import store
from evowiki.sources.glossary import GlossaryEntry, GlossaryPlan
from evowiki.sources.importer import (
    ImportDeps,
    ImportJob,
    SourceDone,
    SourceFailed,
    SourceProgress,
    execute,
)
from evowiki.sources.models import SourceRow
from evowiki.sources.runs import InlineSourceRuns, decode_item, encode_item, workflow_id
from evowiki.sources.summary import SectionSummary
from evowiki.sources.vision import NoVision
from evowiki.wikiapp.llm import LLMUnavailable
from tests.runtime.helpers import make_user

NOW = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)
FIX = Path(__file__).resolve().parents[1] / "fixtures" / "sources"
MIME = {"pdf": "application/pdf", "text": "text/plain", "md": "text/markdown",
        "image": "image/png", "url": "text/html"}


class CountingLLM:
    """A JsonLLM that answers every summary; `fail` raises instead (from the n-th call that is
    not the glossary on). The glossary call returns `glossary` (or raises it)."""

    def __init__(self, fail: BaseException | None = None, fail_from: int = 1,
                 glossary: GlossaryPlan | BaseException | None = None):
        self.calls: list[str] = []
        self.fail, self.fail_from = fail, fail_from
        self.glossary = GlossaryPlan(terms=[]) if glossary is None else glossary
        self.prompts: dict[str, str] = {}

    def __call__(self, role, purpose, prompt, schema):
        self.calls.append(purpose)
        self.prompts[purpose] = prompt
        if purpose == "sources.glossary":
            assert schema is GlossaryPlan
            if isinstance(self.glossary, BaseException):
                raise self.glossary
            return self.glossary
        n = sum(1 for c in self.calls if c != "sources.glossary")
        if self.fail is not None and n >= self.fail_from:
            raise self.fail
        assert schema is SectionSummary, purpose
        return SectionSummary(summary=f"摘要{n}")

    def summaries(self) -> list[str]:
        return [c for c in self.calls if c == "sources.summary"]


def _ids():
    c = count(1)
    return lambda: f"x{next(c):05d}"


def add_source(pool, sid: str, data: bytes, *, kind="pdf", user="u1", origin=None,
               mime=None) -> None:
    row = SourceRow(source_id=sid, user_id=user, kind=kind, title=sid, sha256=sid, bytes=len(data),
                    pages=None, status="reading", error=None, unreadable_pages=(), toc_mode=None,
                    progress={}, origin=origin, created_at=NOW, updated_at=NOW, deleted_at=None)
    with pool.connection() as conn:
        store.insert_source(conn, row, data, mime or MIME[kind])


def src(pool, sid, user="u1"):
    with pool.connection() as conn:
        return store.get_source(conn, user, sid)


@pytest.fixture
def env(pool):
    make_user(pool, "u1")
    make_user(pool, "u2")
    box = {"llm": None}
    ids = _ids()                       # one id space per test: ids are global primary keys

    def deps(**kw) -> ImportDeps:
        base = {"pool": pool, "embedder": HashingEmbedder(), "llm_for": lambda _u: box["llm"],
                "local": NoVision(), "claude": NoVision(),
                "assistant_sees_images": lambda _u: False, "clock": lambda: NOW, "new_id": ids}
        return ImportDeps(**(base | kw))
    return pool, box, deps


def run(deps: ImportDeps, sid: str, user="u1") -> list[tuple[str, object]]:
    runs = InlineSourceRuns(deps)
    assert runs.start(ImportJob(user_id=user, source_id=sid)) == workflow_id(user, sid)
    return list(runs.follow(user, sid))


def stages(items) -> list[str]:
    out: list[str] = []
    for name, p in items:
        key = p.stage if name == "progress" else name
        if not out or out[-1] != key:
            out.append(key)
    return out


def test_outline_pdf_reads_to_ready_without_the_model(env):
    pool, box, deps = env
    box["llm"] = llm = CountingLLM()
    add_source(pool, "s1", (FIX / "outline.pdf").read_bytes())
    items = run(deps(), "s1")
    assert stages(items) == ["parse", "detect", "toc", "usable", "embed", "done"]
    assert all(isinstance(p, SourceProgress) for n, p in items if n == "progress")
    done = items[-1][1]
    assert isinstance(done, SourceDone) and done.status == "ready" and done.pages == 12
    assert done.unreadable_pages == [] and done.note is None
    assert llm.calls == ["sources.glossary"]          # no toc call, no summaries
    row = src(pool, "s1")
    assert (row.status, row.pages, row.toc_mode, row.error) == ("ready", 12, "outline", None)
    assert row.progress["end"] == "done"
    SourceProgress.model_validate(row.progress)     # the list can show it as a progress record
    with pool.connection() as conn:
        secs = store.sections(conn, "u1", "s1")
        chunks = store.chunks_of(conn, "u1", ["s1"])
        n_emb = conn.execute("select count(*) as n from source_section where source_id = 's1' "
                             "and embedding is not null").fetchone()["n"]
    assert done.sections == len(secs) > 0 and chunks and n_emb == len(secs)
    embed = [p for n, p in items if n == "progress" and p.stage == "embed"]
    assert embed[-1].done == embed[-1].total == len(chunks)
    assert embed[-1].message == f"正在整理 {len(chunks)}/{len(chunks)} 段"


def test_mixed_scan_is_partial_with_its_unreadable_pages(env):
    pool, _box, deps = env
    add_source(pool, "s1", (FIX / "mixed_scan.pdf").read_bytes())
    items = run(deps(), "s1")
    done = items[-1][1]
    assert isinstance(done, SourceDone) and done.status == "partial"
    assert done.unreadable_pages == list(range(5, 15))
    row = src(pool, "s1")
    assert row.status == "partial" and row.unreadable_pages == tuple(range(5, 15))
    assert row.toc_mode == "pages" and done.note == "没认出目录，按页分了"


def test_summaries_per_section_then_all_from_the_cache(env, monkeypatch):
    monkeypatch.setenv("EVOWIKI_FULL_TEXT_TOKENS", "1")
    pool, box, deps = env
    box["llm"] = llm = CountingLLM()
    data = (FIX / "outline.pdf").read_bytes()
    add_source(pool, "s1", data)
    items = run(deps(), "s1")
    with pool.connection() as conn:
        secs = store.sections(conn, "u1", "s1")
    readable = [s for s in secs if s.text.strip()]
    assert len(llm.summaries()) == len(readable) > 1
    assert set(llm.calls) == {"sources.glossary", "sources.summary"}
    assert all(s.summary for s in readable)
    summ = [p for n, p in items if n == "progress" and p.stage == "summary"]
    assert len(summ) == len(readable) and summ[-1].done == summ[-1].total == len(readable)
    assert items[-1][0] == "done" and stages(items)[-2] == "summary"

    add_source(pool, "s2", data)                      # the same text again: all cached
    items2 = run(deps(), "s2")
    assert items2[-1][0] == "done" and len(llm.summaries()) == len(readable)
    with pool.connection() as conn:
        assert all(s.summary for s in store.sections(conn, "u1", "s2") if s.text.strip())


def test_no_model_skips_summaries_with_a_note(env, monkeypatch):
    monkeypatch.setenv("EVOWIKI_FULL_TEXT_TOKENS", "1")
    pool, _box, deps = env
    add_source(pool, "s1", (FIX / "outline.pdf").read_bytes())
    done = run(deps(), "s1")[-1][1]
    assert isinstance(done, SourceDone) and done.status == "ready"
    assert done.note == "没有生成摘要（模型不可用）"


def test_a_section_left_without_a_summary_tells_the_learner_nothing(env, monkeypatch):
    """Its readers use its first paragraph; a count of them is nothing he can act on."""
    monkeypatch.setenv("EVOWIKI_FULL_TEXT_TOKENS", "1")
    pool, box, deps = env

    class NoSummary(CountingLLM):
        def __call__(self, role, purpose, prompt, schema):
            if purpose == "sources.summary":
                raise LLMUnavailable("down")
            return super().__call__(role, purpose, prompt, schema)

    box["llm"] = NoSummary()
    add_source(pool, "s1", (FIX / "outline.pdf").read_bytes())
    done = run(deps(), "s1")[-1][1]
    assert isinstance(done, SourceDone) and done.status == "ready" and done.note is None
    with pool.connection() as conn:
        assert not any(s.summary for s in store.sections(conn, "u1", "s1"))


def test_encrypted_pdf_fails_with_a_user_message(env):
    pool, _box, deps = env
    add_source(pool, "s1", (FIX / "encrypted.pdf").read_bytes())
    items = run(deps(), "s1")
    assert items[-1] == ("error", SourceFailed(detail="This material could not be read. Check the file or URL and upload it again.", code="source_parse_failed"))
    assert "done" not in [n for n, _ in items]
    row = src(pool, "s1")
    assert row.status == "failed" and row.error == "这个文件打不开（加了密码？）"


def test_a_402_while_summarizing_keeps_the_source_usable(env, monkeypatch):
    monkeypatch.setenv("EVOWIKI_FULL_TEXT_TOKENS", "1")
    pool, box, deps = env
    box["llm"] = CountingLLM(fail=ProviderRejectedError("402", status_code=402), fail_from=2)
    add_source(pool, "s1", (FIX / "outline.pdf").read_bytes())
    items = run(deps(), "s1")
    assert items[-1] == ("error", SourceFailed(detail="The model service rejected the request. Check its credentials, permissions or balance.", code="provider_rejected"))
    row = src(pool, "s1")
    assert row.status == "ready" and row.progress["end"] == "error"
    with pool.connection() as conn:   # the summary paid for before the 402 is kept
        assert sum(1 for s in store.sections(conn, "u1", "s1") if s.summary) == 1
        assert store.chunks_of(conn, "u1", ["s1"])


class DeletingEmbedder(HashingEmbedder):
    """Deletes the source (another request) on its first call."""

    def __init__(self, pool, sid):
        super().__init__()
        self.pool, self.sid, self.calls = pool, sid, 0

    def embed_documents(self, texts):
        self.calls += 1
        if self.calls == 1:
            with self.pool.connection() as conn:
                store.soft_delete(conn, "u1", self.sid, NOW)
        return super().embed_documents(texts)


def test_deleted_after_the_toc_stops_without_chunks_or_done(env):
    pool, _box, deps = env
    add_source(pool, "s1", (FIX / "outline.pdf").read_bytes())
    d = deps(embedder=DeletingEmbedder(pool, "s1"))
    out: list = []
    from evowiki.agent import durable
    with durable.virtual_workflow("w"):
        status = execute(ImportJob(user_id="u1", source_id="s1"),
                         lambda n, p: out.append((n, p)), d)
    assert status == "ready" and stages(out) == ["parse", "detect", "toc", "usable"]
    with pool.connection() as conn:
        assert conn.execute("select count(*) as n from source_chunk where source_id = 's1'"
                            ).fetchone()["n"] == 0
    # a follower is not left hanging: the end is told from the source's state
    items = run(deps(embedder=DeletingEmbedder(pool, "s1")), "s1")
    assert items[-1] == ("error", SourceFailed(detail="这份资料已经删除了", code="source_deleted"))


def test_text_url_and_image_sources(env):
    pool, _box, deps = env
    add_source(pool, "t1", "# 标题\n\n第一段文字。\n\n## 小节\n\n第二段。".encode(), kind="text")
    done = run(deps(), "t1")[-1][1]
    assert isinstance(done, SourceDone) and done.status == "ready" and done.pages is None
    add_source(pool, "w1", (FIX / "page.html").read_bytes(), kind="url",
               origin="https://example.com/a", mime="text/html; charset=utf-8")
    done = run(deps(), "w1")[-1][1]
    assert isinstance(done, SourceDone) and done.status == "ready" and done.sections >= 1
    add_source(pool, "i1", (FIX / "photo.png").read_bytes(), kind="image")
    done = run(deps(), "i1")[-1][1]
    assert isinstance(done, SourceDone) and done.status == "partial"
    assert done.unreadable_pages == [1]


def test_start_is_idempotent_follow_replays_and_users_are_isolated(env):
    pool, _box, deps = env
    add_source(pool, "s1", (FIX / "outline.pdf").read_bytes())
    runs = InlineSourceRuns(deps())
    job = ImportJob(user_id="u1", source_id="s1")
    assert runs.start(job) == runs.start(job) == "source:u1:s1"
    first = list(runs.follow("u1", "s1"))
    assert first == list(runs.follow("u1", "s1")) and first[-1][0] == "done"
    # another user's source id: nothing to run, the follower is told it is not there
    runs.start(ImportJob(user_id="u2", source_id="s1"))
    assert list(runs.follow("u2", "s1"))[-1] == ("error", SourceFailed(detail="找不到这份资料", code="source_not_found"))
    # a fresh runner (e.g. after a restart) tells the end from the stored state
    again = list(InlineSourceRuns(deps()).follow("u1", "s1"))
    assert again[-1][0] == "done" and again[-1][1] == first[-1][1]


def test_items_round_trip_through_their_own_registry():
    for name, p in [("progress", SourceProgress(stage="toc", done=1, total=1, message="m")),
                    ("done", SourceDone(status="ready", pages=1, sections=2, unreadable_pages=[],
                                        note=None)),
                    ("error", SourceFailed(detail="x"))]:
        assert decode_item(encode_item(name, p)) == (name, p)
    with pytest.raises(ValueError):
        decode_item({"event": "done", "type": "ErrorEvent", "data": {}})


def test_job_is_strict():
    with pytest.raises(ValueError):
        ImportJob(user_id="u1", source_id="s1", extra=1)


def test_a_402_while_building_the_toc_falls_back_to_pages_and_stays_usable(env, monkeypatch):
    monkeypatch.setenv("EVOWIKI_FULL_TEXT_TOKENS", "1")   # summaries would be due: not tried
    pool, box, deps = env
    box["llm"] = llm = CountingLLM(fail=ProviderRejectedError("402", status_code=402))
    add_source(pool, "s1", (FIX / "no_outline.pdf").read_bytes())
    items = run(deps(), "s1")
    assert items[-1] == ("error", SourceFailed(detail="The model service rejected the request. Check its credentials, permissions or balance.", code="provider_rejected"))
    assert llm.calls == ["sources.toc"] and "done" not in [n for n, _ in items]
    row = src(pool, "s1")
    assert (row.status, row.toc_mode, row.error) == ("ready", "pages", None)
    assert row.progress["end"] == "error" and row.progress["stage"] == "toc"
    with pool.connection() as conn:
        assert store.sections(conn, "u1", "s1") and store.chunks_of(conn, "u1", ["s1"])


BUDGET_LINE = "今天的花费到了上限 $1.00，这份资料没有读完。可以在设置里调高。"


def _over_budget() -> BudgetExceededError:
    return BudgetExceededError(Account(user_id="u1", group="page"), "day", 1.0)


def test_the_budget_cap_at_the_toc_falls_back_to_pages_and_stays_usable(env, monkeypatch):
    monkeypatch.setenv("EVOWIKI_FULL_TEXT_TOKENS", "1")   # summaries would be due: not tried
    pool, box, deps = env
    box["llm"] = llm = CountingLLM(fail=_over_budget())
    add_source(pool, "s1", (FIX / "no_outline.pdf").read_bytes())
    items = run(deps(), "s1")
    assert items[-1][0] == "error" and items[-1][1].code == "budget_exceeded"
    assert "This material was not fully read" in items[-1][1].detail
    assert llm.calls == ["sources.toc"] and "done" not in [n for n, _ in items]
    row = src(pool, "s1")
    assert (row.status, row.toc_mode, row.error) == ("ready", "pages", None)
    assert row.progress["end"] == "error" and row.progress["stage"] == "toc"
    assert row.progress["detail"] == BUDGET_LINE
    assert row.progress["error_code"] == "budget_exceeded"
    with pool.connection() as conn:
        assert store.sections(conn, "u1", "s1") and store.chunks_of(conn, "u1", ["s1"])


def test_the_budget_cap_while_summarizing_stops_and_keeps_what_was_written(env, monkeypatch):
    monkeypatch.setenv("EVOWIKI_FULL_TEXT_TOKENS", "1")
    pool, box, deps = env
    box["llm"] = llm = CountingLLM(fail=_over_budget(), fail_from=2)
    add_source(pool, "s1", (FIX / "outline.pdf").read_bytes())
    items = run(deps(), "s1")
    assert items[-1][0] == "error" and items[-1][1].code == "budget_exceeded"
    assert "This material was not fully read" in items[-1][1].detail
    assert len(llm.summaries()) == 2               # stopped at the first refused call
    row = src(pool, "s1")
    assert row.status == "ready" and row.error is None
    assert row.progress["stage"] == "summary" and row.progress["detail"] == BUDGET_LINE
    with pool.connection() as conn:
        assert sum(1 for s in store.sections(conn, "u1", "s1") if s.summary) == 1
        assert store.chunks_of(conn, "u1", ["s1"])


def test_a_summary_failure_is_recorded_at_the_summary_stage(env, monkeypatch):
    monkeypatch.setenv("EVOWIKI_FULL_TEXT_TOKENS", "1")
    pool, box, deps = env
    box["llm"] = CountingLLM(fail=RuntimeError("boom"))
    add_source(pool, "s1", (FIX / "outline.pdf").read_bytes())
    items = run(deps(), "s1")
    assert items[-1][0] == "error"
    row = src(pool, "s1")
    assert row.status == "ready" and row.progress["stage"] == "summary"


def test_a_cancelled_run_without_an_end_says_deleted(env):
    pool, _box, deps = env
    add_source(pool, "s1", (FIX / "outline.pdf").read_bytes())
    runs = InlineSourceRuns(deps(), spawn=lambda _work: None)   # started, never ran
    runs.start(ImportJob(user_id="u1", source_id="s1"))
    runs.cancel("u1", "s1")             # the delete route cancels, then soft-deletes
    assert list(runs.follow("u1", "s1")) == [("error", SourceFailed(detail="这份资料已经删除了", code="source_deleted"))]


def test_decode_item_with_missing_keys_raises_what_follow_catches():
    with pytest.raises((ValueError, KeyError)):
        decode_item({"type": "SourceFailed"})


# ---- materials 3+4 (Task 14): printed pages, bookmark depths, glossary, chunks ----

def _numbered_pdf() -> bytes:
    """6 pages: two unnumbered front pages, then printed pages 1-4 in the footer; flat
    bookmarks whose titles carry two levels of numbers."""
    import io

    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    marks = {3: "1 Intro", 4: "1.1 Problem", 5: "2 Method"}
    for n in range(1, 7):
        if n in marks:
            c.bookmarkPage(f"p{n}")
            c.addOutlineEntry(marks[n], f"p{n}", level=0)
        y = 780
        # the numbered line sits mid-page: edge lines repeated on most pages are stripped
        for line in (marks.get(n, "Front matter"),
                     "Spaced repetition and retrieval practice are discussed here.",
                     f"Let v_{n} be the vector of page {n} in this plain text body.",
                     "A third line of ordinary body text keeps the page readable."):
            c.drawString(60, y, line)
            y -= 18
        if n >= 3:
            c.drawString(300, 40, str(n - 2))
        c.showPage()
    c.save()
    return buf.getvalue()


def store_sections(pool, sid):
    with pool.connection() as conn:
        return store.sections(conn, "u1", sid)


def test_printed_offset_glossary_and_checked_depths_are_stored(env):
    pool, box, deps = env
    box["llm"] = llm = CountingLLM(glossary=GlossaryPlan(terms=[
        GlossaryEntry(term="v_3", means="第 3 页的向量", page=3),
        GlossaryEntry(term=r"\vec{v}", means="不在原文里", page=3)]))
    add_source(pool, "s1", _numbered_pdf())
    items = run(deps(), "s1")
    done = items[-1][1]
    assert isinstance(done, SourceDone) and done.status == "ready" and done.note is None
    row = src(pool, "s1")
    assert row.page_offset == 2
    (entry,) = row.glossary
    assert {k: entry[k] for k in ("term", "means", "page")} == {
        "term": "v_3", "means": "第 3 页的向量", "page": 3}
    assert entry["section_id"] in {s.section_id for s in store_sections(pool, "s1")}
    assert row.glossary_coverage is not None and row.glossary_coverage["whole"]
    assert "v_3" in llm.prompts["sources.glossary"]
    assert "<<<资料内容开始>>>" in llm.prompts["sources.glossary"]
    with pool.connection() as conn:
        secs = store.sections(conn, "u1", "s1")
    assert [(s.title, s.depth) for s in secs] == [("前面的页", 0), ("1 Intro", 0),
                                                  ("1.1 Problem", 1), ("2 Method", 0)]
    assert secs[2].parent_id == secs[1].section_id
    assert any("编号" in d for d in row.progress["diagnostics"])
    # the glossary step comes after the toc and the chunks, before `usable`
    assert stages(items) == ["parse", "detect", "toc", "usable", "embed", "done"]


def test_a_failed_glossary_call_keeps_the_import_and_is_only_logged(env, caplog):
    pool, box, deps = env
    caplog.set_level("WARNING", logger="evowiki.sources.importer")
    box["llm"] = CountingLLM(glossary=RuntimeError("model down"))
    add_source(pool, "s1", (FIX / "outline.pdf").read_bytes())
    done = run(deps(), "s1")[-1][1]
    assert isinstance(done, SourceDone) and done.status == "ready"
    assert done.note is None                       # internal: logged, never shown
    row = src(pool, "s1")
    assert row.glossary == () and row.page_offset is None and row.progress["end"] == "done"
    assert any("s1" in m and "no glossary" in m and "model down" in m for m in caplog.messages)


def test_chunks_never_span_pages(env):
    pool, _box, deps = env
    add_source(pool, "s1", (FIX / "outline.pdf").read_bytes())
    run(deps(), "s1")
    with pool.connection() as conn:
        secs = store.sections(conn, "u1", "s1")
        chunks = store.chunks_of(conn, "u1", ["s1"])
    page_text: dict[int, str] = {}
    for s in secs:
        for part in s.text.split("〔p.")[1:]:
            num, _, body = part.partition("〕")
            page_text[int(num)] = page_text.get(int(num), "") + body
    assert chunks and all(c.page is not None for c in chunks)
    assert all(c.text in page_text[c.page] for c in chunks)


def test_glossary_budget_is_a_typed_stop_with_english_projection(env):
    from evowiki.i18n import language_context
    from evowiki.service.source_schemas import source_out

    pool, box, deps = env
    box["llm"] = llm = CountingLLM(glossary=_over_budget())
    add_source(pool, "s1", (FIX / "outline.pdf").read_bytes())
    with language_context("en"):
        items = run(deps(), "s1")
        event, failed = items[-1]
        assert event == "error" and failed.code == "budget_exceeded"
        assert "This material was not fully read" in failed.detail
        row = src(pool, "s1")
        out = source_out(row)
        assert out.error_code == "budget_exceeded"
        assert out.can_retry
        assert out.status == "ready" and out.error is None
        assert "This material was not fully read" in out.note
    assert llm.calls == ["sources.glossary"]
    assert row.progress["detail"] == BUDGET_LINE
    assert row.progress["error_params"] == {"period": "day", "limit": "1.00"}


def test_old_unstructured_budget_history_is_not_inferred(env):
    from dataclasses import replace

    from evowiki.i18n import language_context
    from evowiki.service.source_schemas import source_out

    pool, _box, deps = env
    add_source(pool, "s1", (FIX / "outline.pdf").read_bytes())
    run(deps(), "s1")
    row = replace(src(pool, "s1"), progress={"end": "error", "detail": BUDGET_LINE})
    with language_context("en"):
        out = source_out(row)
    assert out.error_code is None and out.note == BUDGET_LINE


def test_explicit_resume_checks_real_meter_and_recovers_after_raising_limit(env):
    from evowiki.service.source_schemas import source_out
    from evowiki.sources.uses import next_run
    from evowiki.store.usage import PgUsageMeter, update_settings

    pool, box, deps = env
    account = Account(user_id="u1", group="page")
    meter = PgUsageMeter(pool, clock=lambda: NOW, tz=UTC)
    provider = CountingLLM()
    checks = []

    def metered_fake(role, purpose, prompt, schema):
        checks.append(purpose)
        meter.check(account)
        return provider(role, purpose, prompt, schema)

    box["llm"] = metered_fake
    with pool.connection() as conn:
        update_settings(conn, "u1", now=NOW, daily_limit_usd=0)
        conn.execute("update usage_settings set budget_confirmed=true where user_id='u1'")
    add_source(pool, "s1", (FIX / "outline.pdf").read_bytes())
    runs = InlineSourceRuns(deps())
    runs.start(ImportJob(user_id="u1", source_id="s1"))
    first = list(runs.follow("u1", "s1"))
    assert first[-1][1].code == "budget_exceeded" and provider.calls == []
    assert source_out(src(pool, "s1")).can_retry

    def explicit_retry():
        with pool.connection() as conn:
            job = next_run(conn, "u1", store.get_source(conn, "u1", "s1"), mode="full")
        runs.start(job)
        return list(runs.follow("u1", "s1"))

    blocked = explicit_retry()
    assert blocked[-1][1].code == "budget_exceeded" and provider.calls == []
    assert len(checks) == 2
    with pool.connection() as conn:
        update_settings(conn, "u1", now=NOW, daily_limit_usd=1)
    finished = explicit_retry()
    assert finished[-1][0] == "done" and provider.calls == ["sources.glossary"]
    assert len(checks) == 3
    out = source_out(src(pool, "s1"))
    assert out.status == "ready" and out.error_code is None and out.note is None
    assert not out.can_retry


def test_english_late_crash_keeps_usable_content_and_raw_audit(env, monkeypatch):
    from evowiki.i18n import language_context
    from evowiki.service.source_schemas import source_out
    from evowiki.sources.importer import MSG_LATE_CRASH, _Run
    from evowiki.sources.runs import settled

    pool, _box, deps = env
    add_source(pool, "s1", (FIX / "outline.pdf").read_bytes())
    monkeypatch.setattr(_Run, "embed", lambda self: (_ for _ in ()).throw(RuntimeError("private diagnostic")))
    with language_context("en"):
        items = run(deps(), "s1")
        failed = items[-1][1]
        assert failed.code == "source_processing_failed"
        assert failed.detail == "Processing the material failed. The parts already read remain usable."
        row = src(pool, "s1")
        out = source_out(row)
        assert out.status == "ready" and out.error is None and out.can_retry
        assert out.error_code == failed.code and out.note == failed.detail
        assert settled(pool, "u1", "s1") == items[-1]
    assert row.progress["detail"] == MSG_LATE_CRASH
