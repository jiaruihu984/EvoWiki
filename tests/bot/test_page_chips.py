"""Page chips (rule `chat`, mode "page"): made when a page's suggestions are listed, once per
page and local day, through the gate with a cooldown per chip. Fakes only."""
from datetime import UTC, datetime, timedelta

import pytest

from evowiki.assistant.pg_store import PgSessionStore
from evowiki.assistant.session import SessionInfo
from evowiki.belief.aspects import DefineConcept, Observe
from evowiki.bot.facts import UnknownPage, load_page_facts
from evowiki.bot.pipeline import page_chips
from evowiki.bot.settings import DEFAULT_SETTINGS
from evowiki.goals.contracts import Aspect, Observation, OutlineSection, PageOutline, SectionRole
from evowiki.goals.fold import Page
from evowiki.store import bot as store
from evowiki.wikiapp.events import (
    ConceptName,
    ConceptsNamed,
    PageRecord,
    PageSaved,
    PathRecord,
    PathSaved,
    WikiEvent,
)
from evowiki.wikiapp.repo import Commit, PgRepo
from tests.runtime.helpers import make_user
from tests.wikiapp.test_k1_state import lp

NOW = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)     # 17:00 in Asia/Shanghai
NEXT_DAY = datetime(2026, 10, 15, 1, 0, tzinfo=UTC)  # 09:00 the next local day
_n = iter(range(100_000))
OPEN = "还没懂的：标准差的含义"


def new_id(prefix):
    return f"{prefix}_{next(_n)}"


def page_event(pid, title, cell):
    outline = PageOutline(
        page_id=pid, goal="学统计", concepts=("c1",), targets=("c1",),
        sections=(OutlineSection(id="s1", heading="怎么读", role=SectionRole.EXPLAIN,
                                 cells=(cell,)),))
    rec = PageRecord(page_id=pid, title=title, goal="学统计", concepts=("c1",),
                     targets=("c1",), page=Page(outline=outline, states={}),
                     created_at=NOW - timedelta(days=5))
    return WikiEvent(event_id=f"wev_{pid}", ts=NOW - timedelta(days=5),
                     body=PageSaved(record=rec, action="created"))


@pytest.fixture
def env(pool):
    make_user(pool, "u1")
    repo = PgRepo(pool)
    repo.commit(repo.load("u1"), Commit(aspect=[
        DefineConcept(event_id="ae_def", ts=NOW - timedelta(days=30), concept_id="c1",
                      aspects=(Aspect.INTERPRET, Aspect.PROCEDURE)),
        DefineConcept(event_id="ae_def2", ts=NOW - timedelta(days=30), concept_id="c2",
                      aspects=(Aspect.PROCEDURE,))]))
    repo.commit(repo.load("u1"), Commit(wiki=[
        WikiEvent(event_id="wev_n", ts=NOW - timedelta(days=6), body=ConceptsNamed(names=(
            ConceptName(concept_id="c1", name="均值"), ConceptName(concept_id="c2", name="方差")))),
        page_event("pg_1", "均值", "c1.interpret"), page_event("pg_2", "别的页", "c2.procedure")]))
    return repo


def wrong(repo, cell, ref, ago):
    repo.commit(repo.load("u1"), Commit(aspect=[Observe(
        event_id=f"ae_{next(_n)}", ts=NOW - ago, observation=Observation(
            cell=cell, polarity="~", strength=0.8, kind="check", evidence_ref=ref))]))


def session(pool, sid, page, summary, ago=timedelta(days=1)):
    s = PgSessionStore(pool)
    s.create(SessionInfo(session_id=sid, user_id="u1", page_id=page, note_concept_id=None,
                         created_at=NOW - timedelta(days=3)))
    s.append(sid, f"e_{sid}", "compaction", {"summary": summary}, NOW - ago)


def cards(pool, page="pg_1"):
    with pool.connection() as conn:
        return store.suggestions(conn, "u1", placement="page", page_id=page)


def triggers(pool, since=NOW - timedelta(hours=1)):
    with pool.connection() as conn:
        return store.triggers_since(conn, "u1", since)


def chips(pool, page="pg_1", now=NOW):
    return page_chips(pool, "u1", page, now=now, new_id=new_id)


def settings(pool, **kw):
    with pool.connection() as conn:
        store.put_settings(conn, "u1", DEFAULT_SETTINGS.model_copy(update=kw), NOW)


def test_no_page_chips_are_made_by_default(pool, env):
    """Removed from the UI 2026-10-21: the `chat` rule is not registered and its kind is off by
    default, so listing a page's suggestions makes nothing (no trigger, no card)."""
    assert DEFAULT_SETTINGS.kinds["chat"] is False
    session(pool, "as1", "pg_1", OPEN)
    assert chips(pool) == [] and cards(pool) == []
    assert triggers(pool, NOW - timedelta(days=1)) == []


def test_switching_the_chat_kind_on_still_makes_nothing(pool, env):
    session(pool, "as1", "pg_1", OPEN)
    settings(pool, kinds={**DEFAULT_SETTINGS.kinds, "chat": True})
    assert chips(pool) == [] and cards(pool) == []
    with pytest.raises(UnknownPage):
        chips(pool, page="pg_nope")


def test_page_facts_label_has_the_path_position(pool, env):
    rec = PathRecord(path=lp(2), page_ids=("pg_2", "pg_1"),
                     created_at=NOW - timedelta(days=4))
    env.commit(env.load("u1"), Commit(wiki=[WikiEvent(
        event_id="wev_path", ts=NOW - timedelta(days=4),
        body=PathSaved(record=rec, action="page_started"))]))
    with pool.connection() as conn:
        pf = load_page_facts(conn, "u1", "pg_1", now=NOW, settings=DEFAULT_SETTINGS)
        other = load_page_facts(conn, "u1", "pg_2", now=NOW, settings=DEFAULT_SETTINGS)
    assert pf.label == "均值" and other.label == "别的页"
