import json
from datetime import UTC, datetime, timedelta

import pytest

from evowiki.assistant.pg_store import PgSessionStore
from evowiki.assistant.session import SessionInfo
from evowiki.belief.aspects import AddNote, Claim, DefineConcept, Observe, ResolveClaim
from evowiki.bot.facts import load_facts, new_event_ids, question_key
from evowiki.bot.settings import DEFAULT_SETTINGS
from evowiki.deep.contracts import AssetMeta, RenderCheck
from evowiki.deep.evoui import block_json
from evowiki.deep.store import PgAssetStore, StoredAsset, sha256
from evowiki.goals.contracts import Aspect, Observation
from evowiki.teach.contracts import Signal
from evowiki.wikiapp.events import (
    MatchedPage,
    PageOpened,
    PathRecord,
    PathSaved,
    SignalRecorded,
    SourceMatched,
    WikiEvent,
)
from evowiki.wikiapp.repo import Commit, PgRepo
from tests.runtime.helpers import make_user
from tests.wikiapp.test_block_results import QUIZ
from tests.wikiapp.test_k1_state import lp

NOW = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)
_n = iter(range(100_000))


def test_question_key_forms():
    assert question_key("check:pg_1:s4") == "check:pg_1:s4"
    assert question_key("review:pg_1:s2:rv_7") == "review:pg_1:s2"
    assert question_key("review:-:-:rv_7") == "review:-:-"
    assert (question_key("block:pg_1:s2:ia_q:quiz#0.1:rid.3")
            == "block:pg_1:s2:ia_q:quiz#0.1")
    assert question_key("quiz:5") == "quiz:5"


def obs(ref, *, pol="~", kind="check", cell="c1.interpret", at=NOW, label=None):
    o = Observe(event_id=f"ae{next(_n)}", ts=at, observation=Observation(
        cell=cell, polarity=pol, strength=0.8, kind=kind, evidence_ref=ref))
    note = AddNote(event_id=f"ae{next(_n)}", ts=at, cell=cell, text="x", evidence_ref=ref,
                   label=label)
    return o, note


@pytest.fixture
def repo(pool):
    make_user(pool, "u1")
    r = PgRepo(pool)
    snap = r.load("u1")
    r.commit(snap, Commit(aspect=[DefineConcept(
        event_id="ae_def", ts=NOW - timedelta(days=30), concept_id="c1",
        aspects=(Aspect.INTERPRET, Aspect.PROCEDURE)),
        DefineConcept(event_id="ae_def2", ts=NOW - timedelta(days=30), concept_id="c0",
                      aspects=(Aspect.PROCEDURE,)),
        DefineConcept(event_id="ae_def3", ts=NOW - timedelta(days=30), concept_id="c1",
                      aspects=(Aspect.PROCEDURE,), prereqs=("c0",))]))
    return r


def put(repo, *aspect, wiki=()):
    repo.commit(repo.load("u1"), Commit(aspect=list(aspect), wiki=list(wiki)))


def facts(pool, now=NOW):
    with pool.connection() as conn, conn.transaction():
        return load_facts(conn, "u1", now=now, settings=DEFAULT_SETTINGS)


def test_wrong_answers_window_polarity_revoke_and_same_question(pool, repo):
    a1, n1 = obs("review:pg_1:s2:rv_1", label="题目：甲；选了「乙」", at=NOW - timedelta(hours=3))
    a2, n2 = obs("review:pg_1:s2:rv_2", kind="recall", at=NOW - timedelta(hours=2))
    right, nr = obs("check:pg_1:s4", pol="+", at=NOW - timedelta(hours=1))
    old, no = obs("check:pg_1:s9", at=NOW - timedelta(days=8))
    slip, ns_ = obs("check:pg_1:s5", at=NOW - timedelta(hours=1))
    refuted, nrf = obs("check:pg_1:s6", at=NOW - timedelta(minutes=50))
    learned, nl = obs("check:pg_1:s7", at=NOW - timedelta(minutes=40))
    other, no2 = obs("fb:1", kind="feedback", at=NOW - timedelta(hours=1))
    put(repo, a1, n1, a2, n2, right, nr, old, no, slip, ns_, refuted, nrf, learned, nl, other, no2)
    def claim(eid, kind, src):
        return Claim(event_id=eid, ts=NOW, claim=kind, cell="c1.interpret", source_event_id=src)

    put(repo, claim("ae_c1", "slip", slip.event_id), claim("ae_c2", "slip", refuted.event_id),
        claim("ae_c3", "learned", learned.event_id))
    put(repo, ResolveClaim(event_id="ae_r1", ts=NOW, claim_event_id="ae_c2", outcome="refuted"))
    got = facts(pool).wrong_answers
    assert [w.event_id for w in got] == [a1.event_id, a2.event_id, refuted.event_id,
                                         learned.event_id]
    assert [w.question for w in got][:2] == ["review:pg_1:s2", "review:pg_1:s2"]
    assert got[0].cell == "c1.interpret" and got[0].at == NOW - timedelta(hours=3)
    assert got[0].misconception is None


def test_block_misconception_resolved_from_asset(pool, repo):
    content = block_json(QUIZ)
    meta = AssetMeta(asset_id="ia_q", page_id="pg_1", section_id="s2", interactive_id="z-quiz",
                     title="标准分", height=400, sha256=sha256(content), kind="evoui",
                     check=RenderCheck(interactive_id="z-quiz", ok=True, probes_passed=0,
                                       probes_total=0))
    with pool.connection() as conn:
        PgAssetStore(pool).writer([StoredAsset(user_id="u1", meta=meta, html=content,
                                               job_id="j", created_at=NOW)])(conn)
    good, ng = obs("block:pg_1:s2:ia_q:quiz#0.0:rid.0", kind="quiz")
    nocase, nn = obs("block:pg_1:s2:ia_q:quiz#0.7:rid.1", kind="quiz")
    missing, nm = obs("block:pg_1:s2:ia_nope:quiz#0.0:rid.2", kind="quiz")
    put(repo, good, ng, nocase, nn, missing, nm)
    v1, nv = obs("block:pg_1:s2:ia_q:0.0:br_1", kind="quiz")
    v1b, nvb = obs("block:pg_1:s2:ia_q:0.0:br_2", kind="quiz")
    v1x, nvx = obs("block:pg_1:s2:ia_nope:0.0:br_3", kind="quiz")
    put(repo, v1, nv, v1b, nvb, v1x, nvx)
    got = {w.event_id: w for w in facts(pool).wrong_answers}
    assert got[v1.event_id].misconception == "sign"
    assert got[v1.event_id].question == got[good.event_id].question   # v1 and v2 refs agree
    assert got[v1b.event_id].question == got[v1.event_id].question
    assert got[v1x.event_id].question == "block:pg_1:s2:ia_nope:0.0"  # unreadable: raw key
    assert got[good.event_id].misconception == "sign"
    assert got[good.event_id].question == "block:pg_1:s2:ia_q:quiz#0.0"
    assert got[nocase.event_id].misconception is None
    assert got[missing.event_id].misconception is None


def test_signals_keep_last_twenty_teaching_nonzero_oldest_first(pool, repo):
    """Only signals about the explanation (strategy_negative v2); answers are left out, so a run
    of answers never pushes them out of the window."""
    evs = []
    for i in range(50):
        if i % 2:
            kind, outcome, text = "check_answer", 0.0, ""
        elif i % 10 == 0:
            kind, outcome, text = "card_feedback", None, "ok"          # polarity 0: left out
        else:
            kind, outcome, text = "page_feedback", None, "讲得太快" if i % 4 else ""
        evs.append(WikiEvent(event_id=f"wev_s{i}", ts=NOW + timedelta(seconds=i), body=SignalRecorded(
            signal=Signal(signal_id=f"sig{i}", kind=kind, at=NOW + timedelta(seconds=i),
                          text=text, outcome=outcome))))
    put(repo, wiki=evs)
    sigs = facts(pool).signals
    assert len(sigs) == 20 and sigs[-1].event_id == "wev_s48"
    assert [s.at for s in sigs] == sorted(s.at for s in sigs)
    assert all(s.polarity == -1 and s.kind == "page_feedback" for s in sigs)
    assert {s.event_id: s.written for s in sigs}["wev_s48"] is False   # 48 % 4 == 0: no text
    assert {s.event_id: s.written for s in sigs}["wev_s46"] is True


def test_new_event_ids_after_cursor(pool, repo):
    a, n = obs("check:pg_1:s4")
    sig = WikiEvent(event_id="wev_x", ts=NOW, body=SignalRecorded(signal=Signal(
        signal_id="sg", kind="check_answer", at=NOW, outcome=0.0)))
    put(repo, a, n, wiki=[sig])
    snap = repo.load("u1")
    with pool.connection() as conn:
        ids, w, asp = new_event_ids(conn, "u1", 0, 0)
        assert {"wev_x", "ae_def", a.event_id, n.event_id} <= ids
        assert (w, asp) == (snap.wiki_seq, snap.aspect_seq)
        assert new_event_ids(conn, "u1", w, asp) == (frozenset(), w, asp)
        assert new_event_ids(conn, "u1", w, asp - 1)[0] == {n.event_id}



def _v7(unclear=(), pending=()):
    """A compact@v7 compaction payload (assistant/summary.py)."""
    return {"version": "compact@v7",
            "handoff": {"summary": "聊了 z", "pending": [{"task": t} for t in pending]},
            "learning": {"unclear": [{"concept": c, "evidence_id": f"m{i}"}
                                     for i, c in enumerate(unclear)]},
            "edits": [], "first_kept": None}

def test_goals_open_questions_and_due(pool, repo):
    rec = PathRecord(path=lp(2), page_ids=("pg_1", None),
                     created_at=NOW - timedelta(days=5))
    opened = NOW - timedelta(days=4)
    put(repo, wiki=[
        WikiEvent(event_id="wev_p", ts=NOW - timedelta(days=5), body=PathSaved(
            record=rec, action="page_started")),
        WikiEvent(event_id="wev_o", ts=opened, body=PageOpened(page_id="pg_1"))])
    store = PgSessionStore(pool)
    store.create(SessionInfo(session_id="as1", user_id="u1", page_id="pg_1", note_concept_id=None,
                             created_at=NOW - timedelta(days=3)))
    store.create(SessionInfo(session_id="as2", user_id="u1", page_id="pg_2", note_concept_id=None,
                             created_at=NOW - timedelta(days=3)))
    store.append("as1", "e1", "compaction", _v7(unclear=["z 的单位"]), NOW - timedelta(days=3))
    store.append("as1", "e2", "compaction", _v7(unclear=["标准差的含义"]), NOW - timedelta(days=2))
    store.append("as2", "e3", "compaction", _v7(), NOW - timedelta(days=2))
    f = facts(pool)
    assert len(f.goals) == 1
    g = f.goals[0]
    assert (g.path_id, g.title, g.next_page_id, g.last_opened) == ("path-k", "学统计", "pg_1",
                                                                  opened)
    assert len(f.open_questions) == 1
    q = f.open_questions[0]
    assert (q.session_id, q.page_id, q.text) == ("as1", "pg_1", "标准差的含义")
    assert q.at == NOW - timedelta(days=2) and q.page_opened_since is False
    assert f.due_review.count == 0 and f.due_review.cells == ()
    assert json.dumps(f.names)    # JSON-friendly


def test_open_questions_read_only_checked_v7_unclear_never_to_dos_or_old_entries(pool, repo):
    """The Bot's 还没懂 chips come from compact@v7 `learning.unclear` (evidence checked when it
    was written), never from `handoff.pending` (a question the assistant asked, a page edit
    waiting for him) and never from an entry before v7 (whose 还没懂的 mixed those in)."""
    put(repo, wiki=[WikiEvent(event_id="wev_o", ts=NOW - timedelta(days=4),
                              body=PageOpened(page_id="pg_1"))])
    v6 = "问了什么：\n- 好\n还没懂的：\n- 等用户提供两道重复的题\n改过哪里：无"
    store = PgSessionStore(pool)
    for sid, payload in (
            ("as1", _v7(unclear=["负的 z", "z 的单位"],
                        pending=["第 2 题还没答", "等他确认「均值」一节的修改"])),
            ("as2", {"summary": v6, "fields": {"还没懂的": ["等用户提供两道重复的题"]},
                     "first_kept": None}),
            ("as3", _v7(pending=["等他提供两道重复的题"]))):
        store.create(SessionInfo(session_id=sid, user_id="u1", page_id="pg_1",
                                 note_concept_id=None, created_at=NOW - timedelta(days=3)))
        store.append(sid, f"e_{sid}", "compaction", payload, NOW - timedelta(days=2))
    f = facts(pool)
    assert [(q.session_id, q.text) for q in f.open_questions] == [("as1", "负的 z；z 的单位")]


def test_source_matches_last_fourteen_days(pool, repo):
    def sm(sid, days, pages):
        body = SourceMatched(source_id=sid, title=f"资料{sid}", pages=tuple(
            MatchedPage(page_id=p, title=f"页{p}", section_ids=("s",)) for p in pages))
        return WikiEvent(event_id=f"source_matched:u1:{sid}", ts=NOW - timedelta(days=days),
                         body=body)
    put(repo, wiki=[sm("new", 1, ["a", "b"]), sm("old", 20, ["a"]), sm("none", 2, [])])
    f = facts(pool)
    [m] = f.source_matches
    assert (m.event_id, m.source_id, m.title) == ("source_matched:u1:new", "new", "资料new")
    assert m.pages == (("a", "页a"), ("b", "页b"))


def test_load_facts_passes_the_frequent_exam_nodes_to_the_review_schedule(pool, repo, monkeypatch):
    from evowiki.bot import facts as facts_mod

    seen = {}
    real = facts_mod.schedule

    def spy(*a, **kw):
        seen.update(kw)
        return real(*a, **kw)

    monkeypatch.setattr(facts_mod, "schedule", spy)
    monkeypatch.setattr(facts_mod, "exam_counts", lambda conn, uid: {"n1": 3, "n2": 2})
    facts(pool)
    assert seen["exam_concepts"] == frozenset({"n1"})
