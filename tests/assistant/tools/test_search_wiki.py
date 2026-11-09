from datetime import UTC, datetime
from pathlib import Path

from evowiki.assistant.tools.search_wiki import (
    SearchWikiTool,
    SegmentHit,
    VectorContentSearch,
    score_text,
    snippet,
)

SNAP = Path(__file__).resolve().parents[2] / "fixtures" / "runtime" / "content_snapshot.json"


class FakeContent:
    def __init__(self, hits=(), boom=False):
        self.hits, self.boom, self.calls = list(hits), boom, []

    def search(self, query, k):
        self.calls.append((query, k))
        if self.boom:
            raise RuntimeError("pg down")
        return self.hits[:k]


def test_terms_scoring_and_snippet():
    assert score_text("z-score 标准差", "标准差是 z-score 的分母") >= 2
    assert score_text("傅里叶", "完全无关") == 0
    s = snippet("标准差", "前面很长" * 100 + "标准差在这里" + "后面" * 100, limit=60)
    assert "标准差" in s and len(s) <= 60


def test_finds_user_pages_and_content(store, ctx):
    content = FakeContent([SegmentHit(segment_id="seg1", title="z 分数 · 定义",
                                      text="z 分数表示离均值几个标准差。", score=0.8)])
    r = SearchWikiTool(store, content).run({"query": "怎么算 z-score"}, ctx)
    assert r.error is None
    assert "[W1]" in r.content and "z-score 怎么算" in r.content and "[C1]" in r.content
    assert r.sources[0].page_id == "p1" and r.sources[-1].segment_id == "seg1"
    assert content.calls == [("怎么算 z-score", 5)]


def test_only_this_users_pages(store, ctx):
    r = SearchWikiTool(store).run({"query": "z-score"}, ctx)   # p9 (u2) also has a z-score section
    assert r.sources and all(s.page_id != "p9" for s in r.sources)


def test_title_only_match_surfaces_page(ctx):
    from evowiki.wikiapp.page_reader import InMemoryPageStore
    from tests.assistant.tools.conftest import make_page
    pages = InMemoryPageStore({"u1": [make_page("p5", "复习傅里叶变换")]})
    r = SearchWikiTool(pages).run({"query": "傅里叶"}, ctx)
    assert r.error is None and "[W1] 《复习傅里叶变换》（page_id=p5）" in r.content
    assert "z-score 怎么算" in r.content          # snippet lists the section headings
    assert [s.page_id for s in r.sources] == ["p5"] and r.sources[0].segment_id is None


def test_content_down_tail_survives_clipping(ctx):
    from evowiki.agent.tool_helpers import SEARCH_CHARS
    from evowiki.wikiapp.page_reader import InMemoryPageStore
    from tests.assistant.tools.conftest import make_page
    many = InMemoryPageStore({"u1": [make_page(f"p{i}", "z-score " + "长标题" * 300,
                                               body2="z-score " * 200) for i in range(8)]})
    r = SearchWikiTool(many, FakeContent(boom=True)).run({"query": "z-score", "limit": 8}, ctx)
    assert len(r.content) <= SEARCH_CHARS and r.content.endswith("（内容库暂时不可用）")
    shown = r.content.removesuffix("\n（内容库暂时不可用）")
    assert shown.endswith("…")                       # whole blocks only: last snippet intact
    assert not shown.endswith("字]")                 # (no clip marker at the end)
    assert 0 < len(r.sources) < 8
    assert r.content.count("[W") == len(r.sources)       # a source per block the model saw


def test_sources_only_for_blocks_shown(ctx):
    from evowiki.agent.tool_helpers import SEARCH_CHARS
    from evowiki.wikiapp.page_reader import InMemoryPageStore
    from tests.assistant.tools.conftest import make_page
    many = InMemoryPageStore({"u1": [make_page(f"p{i}", "z-score " + "长标题" * 300,
                                               body2="z-score " * 200) for i in range(8)]})
    hits = [SegmentHit(segment_id=f"seg{i}", title="t" * 300, text="z-score " * 200, score=0.5)
            for i in range(8)]
    r = SearchWikiTool(many, FakeContent(hits)).run({"query": "z-score", "limit": 8}, ctx)
    assert r.error is None and len(r.content) <= SEARCH_CHARS
    assert 0 < len(r.sources) < 16
    for s in r.sources:
        assert (f"page_id={s.page_id}" if s.page_id else f"segment_id={s.segment_id}") in r.content
    shown_pages = {s.page_id for s in r.sources if s.page_id}
    assert all(f"page_id=p{i}" not in r.content for i in range(8) if f"p{i}" not in shown_pages)


def test_single_latin_letter_matches_whole_word_only(store, ctx):
    assert score_text("z", "zebra crossing") == 0
    assert score_text("z", "z-score 怎么算") == 1
    assert score_text("z", "z分数") == 1
    r = SearchWikiTool(store).run({"query": "z"}, ctx)
    assert r.error is None and "z-score 怎么算" in r.content


def test_single_letter_does_not_match_inside_words(ctx):
    from evowiki.wikiapp.page_reader import InMemoryPageStore
    from tests.assistant.tools.conftest import make_page
    wp = make_page("p7", "动物", body2="zebra 斑马")
    outline = wp.page.outline.model_copy(update={"sections": tuple(
        sec.model_copy(update={"heading": f"第{i}段"})
        for i, sec in enumerate(wp.page.outline.sections))})
    wp = wp.model_copy(update={"page": wp.page.model_copy(update={"outline": outline})})
    r = SearchWikiTool(InMemoryPageStore({"u1": [wp]})).run({"query": "z"}, ctx)
    assert r.error is None and "没有找到" in r.content


def test_empty_titles_fall_back_to_ids(ctx):
    from evowiki.wikiapp.page_reader import InMemoryPageStore
    from tests.assistant.tools.conftest import make_page
    wp = make_page().model_copy(update={"title": ""})
    content = FakeContent([SegmentHit(segment_id="seg1", title="", text="z-score 定义", score=0.8)])
    r = SearchWikiTool(InMemoryPageStore({"u1": [wp]}), content).run({"query": "z-score"}, ctx)
    assert r.error is None and r.sources and all(s.title for s in r.sources)
    assert r.sources[-1].title == "seg1"


def test_spec_states_limit_and_limit_described():
    from evowiki.wikiapp.page_reader import InMemoryPageStore
    s = SearchWikiTool(InMemoryPageStore({})).spec
    assert "8" in s.description and "4000" in s.description and len(s.description) <= 300
    assert s.parameters["properties"]["limit"].get("description")


def test_vector_content_search_drops_unknown_and_flagged(monkeypatch):
    from types import SimpleNamespace as NS

    from evowiki.assistant.tools import search_wiki as mod
    from evowiki.content.models import ReviewStatus, SegmentKind

    def seg(sid, status):
        return NS(id=sid, concept_id="c1", kind=SegmentKind.DEFINITION, review_status=status,
                  body=f"body of {sid}")
    segs = [seg("bad", ReviewStatus.FLAGGED), seg("a", ReviewStatus.AUTO_VERIFIED),
            seg("b", ReviewStatus.HUMAN_APPROVED), seg("c", ReviewStatus.AUTO_VERIFIED)]
    graph = NS(segments=tuple(segs), concept=lambda cid: NS(name_zh="概念一"))
    content = NS(version_id="cv1", graph=graph)
    calls = []

    def fake_search(conn, cv, embedder, query, *, k, lang=None):
        calls.append((cv, query, k))
        return [("ghost", 0.99), ("bad", 0.95), ("a", 0.9), ("b", 0.8), ("c", 0.7)]
    monkeypatch.setattr(mod, "search_segments", fake_search)
    hits = VectorContentSearch(object(), content, object()).search("q", 2)
    assert calls == [("cv1", "q", 4)]
    assert [h.segment_id for h in hits] == ["a", "b"]
    assert hits[0].title == "概念一 · definition" and hits[0].text == "body of a"


def test_nothing_found_is_not_an_error(store, ctx):
    r = SearchWikiTool(store, FakeContent()).run({"query": "傅里叶变换"}, ctx)
    assert r.error is None and "没有找到" in r.content and r.sources == ()


def test_content_failure_degrades(store, ctx):
    r = SearchWikiTool(store, FakeContent(boom=True)).run({"query": "z-score"}, ctx)
    assert r.error is None and "[W1]" in r.content and "内容库暂时不可用" in r.content


def test_page_store_failure_is_error(ctx):
    class Broken:
        def all(self, user_id):
            raise RuntimeError("db down")
        def get(self, user_id, page_id):
            raise RuntimeError("db down")
    assert SearchWikiTool(Broken()).run({"query": "x"}, ctx).error


def test_limits(ctx):
    from evowiki.wikiapp.page_reader import InMemoryPageStore
    from tests.assistant.tools.conftest import make_page
    many = InMemoryPageStore({"u1": [make_page(f"p{i}", f"z-score 第{i}页",
                                               body2="z-score " * 2000) for i in range(20)]})
    t = SearchWikiTool(many)
    r = t.run({"query": "z-score", "limit": 8}, ctx)
    assert len(r.sources) <= 8 and len(r.content) <= 4000
    assert t.run({"query": ""}, ctx).error
    assert t.run({"query": "x", "limit": 9}, ctx).error


def test_vector_content_search_keeps_reviewed_only(pool):
    from evowiki.content.models import PUBLISHABLE
    from evowiki.retrieval.embed import HashingEmbedder
    from evowiki.store.content import import_snapshot_file, load_content
    from evowiki.store.vectors import index_segments
    e = HashingEmbedder()
    with pool.connection() as conn:
        cv = import_snapshot_file(conn, SNAP, datetime(2026, 10, 2, tzinfo=UTC))
        lc = load_content(conn, cv)
        index_segments(conn, lc, e)
        target = lc.graph.segments_for("pca")[0]
        hits = VectorContentSearch(conn, lc, e).search(target.body, 3)
    assert 0 < len(hits) <= 3
    by_id = {s.id: s for s in lc.graph.segments}
    assert all(by_id[h.segment_id].review_status in PUBLISHABLE for h in hits)
    assert all(h.title and h.text for h in hits)
