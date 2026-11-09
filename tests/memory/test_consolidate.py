# Offline cases of the purpose assistant.consolidate (tests/llm_cases/inventory.py).
import json
from datetime import UTC, datetime, timedelta
from itertools import count

import pytest

from evowiki.assistant.pg_store import PgSessionStore
from evowiki.assistant.session import MemorySessionStore, SessionInfo, fold
from evowiki.content.models import Lang
from evowiki.llm.gateway import Gateway
from evowiki.llm.log import CallLog
from evowiki.llm.models import GatewayConfig, ModelRef
from evowiki.llm.scripted import ScriptedProvider
from evowiki.memory.consolidate import (
    CONSOLIDATE_PURPOSE,
    Candidate,
    Candidates,
    candidate_key,
    consolidate,
    retry_pending_landings,
)
from evowiki.memory.contracts import EvidenceRef
from evowiki.memory.index import CORRECTED_ANSWER
from evowiki.memory.lander import Deferred, Landed, NullPreferenceLander, NullRuleGuard, Refused
from evowiki.memory.store import Episode, InMemoryMemoryStore, Item, PgMemoryStore, Suppression
from evowiki.profile.note import scoped_line
from evowiki.store.users import UserRow, create_user

T = datetime(2026, 10, 27, 9, tzinfo=UTC)
REF = ModelRef(provider="scripted", model="m")
CFG = GatewayConfig(generator=REF, verifier=REF, allow_single_provider=True)

# s1 (u1): m1/a1 proposal, m2/a2 his confirmation, m3/a3 a preference, m4/a4 an open question,
# m5/a5 the assistant suggesting a direction
TURNS = [("m1", "atomic 应该怎么定？", "a1", "建议 atomic 由知识本身决定，不按层级强制。"),
         ("m2", "可以，就这样", "a2", "好，就这么定。"),
         ("m3", "以后数学题都先解释符号", "a3", "好的，以后先解释符号。"),
         ("m4", "为什么除以标准差我还没懂", "a4", "下次再细讲。"),
         ("m5", "嗯", "a5", "你可以以后看看贝叶斯。")]


def _session(store, sid, uid, turns, *, at=T):
    store.create(SessionInfo(session_id=sid, user_id=uid, page_id="p1", note_concept_id=None,
                             created_at=at))
    n = count(1)
    for i, (m, u, a, ans) in enumerate(turns):
        t = at + timedelta(minutes=i)
        store.append(sid, f"{sid}-e{next(n)}", "user", {"message_id": m, "text": u,
                                                       "selection": None}, t)
        store.append(sid, f"{sid}-e{next(n)}", "assistant", {"message_id": a, "text": ans,
                                                            "sources": []}, t)


def _gateway(tmp_path, reply, seen=None):
    def respond(model, request):
        if seen is not None:
            seen.append(request)
        out = reply(request) if callable(reply) else reply
        return json.dumps(out if isinstance(out, dict) else {"items": out}, ensure_ascii=False)
    return Gateway(CFG, CallLog(tmp_path / f"c{next(_n)}.jsonl"), mode="record",
                   providers={"scripted": ScriptedProvider("scripted", respond)})


_n = count(1)


def ev(mid, role="user", quote=None, sid=None):
    d = {"message_id": mid, "role": role}
    if quote is not None:
        d["quote"] = quote
    if sid is not None:
        d["session_id"] = sid
    return d


def cand(kind, content, *evidence, **kw):
    return {"kind": kind, "content": content, "scope_type": "global", "evidence": list(evidence),
            **kw}


DECISION = cand("decision", "atomic 由知识本身决定", ev("a1", "assistant"),
                ev("m2", quote="可以，就这样"))


class World:
    def __init__(self, tmp_path):
        self.tmp_path = tmp_path
        self.sessions = MemorySessionStore()
        self.mem = InMemoryMemoryStore()
        _session(self.sessions, "s1", "u1", TURNS)
        _session(self.sessions, "s9", "u2", [("x1", "别人说的可以就这样", "y1", "好")])
        self.ids = count(1)

    def run(self, cands, *, lander=None, guard=None, first=0, through=5, sid="s1", seen=None):
        state = fold(self.sessions.info(sid), self.sessions.entries(sid))
        return consolidate(state, sessions=self.sessions, store=self.mem,
                           gateway=_gateway(self.tmp_path, cands, seen), account=None,
                           lander=lander or NullPreferenceLander(), guard=guard or NullRuleGuard(),
                           first=first, through=through, now=T + timedelta(hours=1),
                           new_id=lambda p: f"{p}{next(self.ids)}")

    def entries(self, sid="s1"):
        return [e.payload for e in self.sessions.entries(sid) if e.type == "consolidation"]


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def reasons(r):
    return [why for _, why in r.dropped]


def test_a_line_its_quote_does_not_say_is_dropped(world):
    """The real case: 「口腔科住院医」, taken from a recalled answer, pinned on an unrelated quote."""
    r = world.run([cand("about_me", "口腔科住院医", ev("m3", quote="以后数学题都先解释符号"))],
                  lander=NullPreferenceLander())
    assert reasons(r) == ["unbacked"]


def test_a_fact_said_of_one_domain_keeps_it_in_its_profile_line(world):
    """The real case: 「python\\n背景：什么都不会」 became 「用户背景：什么都不会」 under 我是谁."""
    world.run([cand("about_me", "先解释符号", ev("m3", quote="以后数学题都先解释符号"), topic="python")],
              lander=NullPreferenceLander())
    [entry] = world.entries()
    assert entry["pending_landing"][0]["candidate"]["content"] == "python：先解释符号"
    assert scoped_line("线性代数（矩阵运算）", "线性代数学过，矩阵运算很熟") == "线性代数学过，矩阵运算很熟"
    assert scoped_line("Python", "python 零基础") == "python 零基础"
    assert scoped_line(None, "大二学生") == "大二学生"


def test_topic_is_required_of_the_model_and_a_stored_candidate_without_it_still_reads():
    assert "topic" in Candidates.model_json_schema()["$defs"]["Candidate"]["required"]
    assert Candidate.model_validate(cand("about_me", "大二学生", ev("m2", quote="可以"))).topic is None


def test_a_decision_without_his_words_is_dropped(world):
    r = world.run([cand("decision", "atomic 放 ConceptName", ev("a1", "assistant"))])
    assert r.items_written == [] and reasons(r) == ["not_from_user"]


def test_proposal_plus_next_turn_confirmation_is_a_decision(world):
    r = world.run([DECISION])
    [it] = r.items_written
    assert (it.kind, it.status, it.provenance) == ("decision", "active", "explicit_history")
    assert [e.message_id for e in it.evidence] == ["m2", "a1"]     # his yes, then the plan
    assert world.mem.items("u1")[0].item_id == it.item_id
    [entry] = world.entries()
    assert entry["through_message_id"] == "m5" and entry["item_ids"] == [it.item_id]


def test_a_bare_yes_takes_the_assistant_line_before_it_not_the_models_pick(world):
    r = world.run([cand("decision", "atomic 由知识本身决定", ev("a2", "assistant"),
                        ev("m2", quote="可以，就这样"))])
    [it] = r.items_written
    assert [e.message_id for e in it.evidence] == ["m2", "a1"]


def test_a_bare_yes_with_nothing_before_it_has_no_content(world):
    _session(world.sessions, "s3", "u1", [("k1", "可以", "k2", "嗯")])
    r = world.run([cand("decision", "atomic 由知识本身决定", ev("k1", quote="可以"))], sid="s3",
                  through=1)
    assert reasons(r) == ["no_content"]          # his whole message 「可以」: a quote, no content
    r = world.run([cand("decision", "以后都用中文讲", ev("m1", quote="可"))])
    assert reasons(r) == ["quote_mismatch"]      # a piece of a message stays ≥ 4 characters


def test_his_own_decision_stands_on_his_words_wherever_the_assistant_line_is(world):
    """Live run C: he said the decision, the assistant answered 「可以」 after it."""
    r = world.run([cand("decision", "以后数学题先解释符号", ev("m3", quote="以后数学题都先解释符号"),
                        ev("a3", "assistant"))])
    [it] = r.items_written
    assert [e.message_id for e in it.evidence] == ["m3", "a3"]


def test_a_decision_restating_a_landed_preference_is_left_to_it(tmp_path):
    pref = cand("preference", "以后数学题都先解释符号", ev("m3", quote="以后数学题都先解释符号"))
    dec = cand("decision", "数学题都先解释符号", ev("m3", quote="以后数学题都先解释符号"))
    r = World(tmp_path).run([dec, pref], lander=FakeLander(Landed(pending_action_id="act1")))
    assert [i.kind for i in r.items_written] == ["preference"]
    assert reasons(r) == ["same_as_preference"]
    r = World(tmp_path).run([dec, pref], lander=FakeLander(Refused("no")))
    assert [i.kind for i in r.items_written] == ["decision"]     # nothing landed: it stays


def test_quote_not_in_that_message_drops(world):
    r = world.run([cand("decision", "x", ev("a1", "assistant"), ev("m2", quote="不行不行不行"))])
    assert reasons(r) == ["quote_mismatch"]


def test_user_message_without_quote_drops(world):
    r = world.run([cand("open_question", "为什么除以标准差", ev("m4"))])
    assert reasons(r) == ["quote_mismatch"]


def test_assistant_suggested_direction_is_not_an_open_question(world):
    r = world.run([cand("open_question", "以后看看贝叶斯", ev("a5", "assistant"))])
    assert reasons(r) == ["not_from_user"]


def test_open_question_from_his_words(world):
    r = world.run([cand("open_question", "为什么除以标准差", ev("m4", quote="为什么除以标准差"))])
    assert [i.kind for i in r.items_written] == ["open_question"]


def test_every_evidence_outside_the_range_drops(world):
    world.run([], through=2)                 # turns 0..1 (a1 / m2) are consolidated already
    r = world.run([DECISION])
    assert reasons(r) == ["evidence_missing"]


def test_no_watermark_starts_at_the_first_turn_whatever_first_says(world):
    seen = []
    world.run([], first=3, seen=seen)
    assert "[m1] 用户：" in seen[0].prompt


def test_a_failed_segment_is_taken_by_the_next_call(world, tmp_path):
    state = fold(world.sessions.info("s1"), world.sessions.entries("s1"))
    gw = Gateway(CFG, CallLog(tmp_path / "bad2.jsonl"), mode="record",
                 providers={"scripted": ScriptedProvider("scripted", lambda m, r: "not json")})
    with pytest.raises(ValueError):
        consolidate(state, sessions=world.sessions, store=world.mem, gateway=gw, account=None,
                    lander=NullPreferenceLander(), guard=NullRuleGuard(), first=0, through=2,
                    now=T, new_id=lambda p: p + "x")
    seen = []
    r = world.run([DECISION], first=2, seen=seen)        # the next fold: first=2, cut=5
    assert "[m1] 用户：" in seen[0].prompt and len(r.items_written) == 1


def test_a_long_backlog_is_taken_in_pieces(world, monkeypatch):
    import evowiki.memory.consolidate as mod

    monkeypatch.setattr(mod, "SEGMENT_TURNS_MAX", 2)
    world.run([])
    assert world.entries()[-1]["through_message_id"] == "m2"
    world.run([])
    assert world.entries()[-1]["through_message_id"] == "m4"


def test_overlapping_run_that_lost_the_race_writes_nothing(world):
    inner = []

    def reply(request):
        if not inner:
            inner.append(world.run([DECISION], through=2))   # commits turns 0..1 meanwhile
        return [DECISION]
    r = world.run(reply, through=5)
    assert len(inner[0].items_written) == 1
    assert r.committed is False and r.items_written == []
    assert len(world.mem.items("u1")) == 1


def test_a_settled_key_is_never_landed_twice_by_concurrent_retries(world):
    world.run([PREF])
    key = world.entries()[0]["pending_landing"][0]["candidate_key"]
    payload = {"landed": [key], "refused": []}
    assert world.mem.commit_consolidation(world.sessions, session_id="s1", entry_id="r1",
                                          payload=payload, items=(), status_changes=(), at=T)
    assert not world.mem.commit_consolidation(world.sessions, session_id="s1", entry_id="r2",
                                              payload=payload, items=(), status_changes=(), at=T)


def test_missing_message_or_wrong_role_drops(world):
    r = world.run([cand("decision", "x", ev("zz", quote="可以，就这样"))])
    assert reasons(r) == ["evidence_missing"]
    world2 = World(world.tmp_path)
    r = world2.run([cand("decision", "x", ev("m2", "assistant"))])
    assert reasons(r) == ["evidence_missing"]


def test_foreign_user_evidence_drops(world):
    r = world.run([cand("decision", "x", ev("m2", quote="可以，就这样"),
                        ev("x1", quote="可以就这样", sid="s9"))])
    assert reasons(r) == ["evidence_foreign"]


def test_cross_segment_confirmation(world):
    # turns 0..1 were consolidated already (proposal a1 in it); m2 ... no: a1 earlier, m2 here
    world.run([], through=1)
    assert world.entries()[0]["through_message_id"] == "m1"
    r = world.run([DECISION], first=0, through=2)
    assert len(r.items_written) == 1


def test_cross_session_proposal_with_confirmation_here(world):
    _session(world.sessions, "s0", "u1", [("p1", "atomic 呢", "q1", "建议放在 ConceptName 上")],
             at=T - timedelta(days=1))
    r = world.run([cand("decision", "atomic 放在 ConceptName 上", ev("q1", "assistant", sid="s0"),
                        ev("m2", quote="可以，就这样"))])
    [it] = r.items_written       # a bare yes: the plan is the line right before it, in its log
    assert {(e.session_id, e.message_id) for e in it.evidence} == {("s1", "a1"), ("s1", "m2")}


def test_suppressed_evidence_drops(world):
    world.mem._suppressions["old"] = ("u1", Suppression(item_id="old", kind="decision",
                                                        evidence_message_ids=frozenset({"m2"})))
    r = world.run([DECISION])
    assert reasons(r) == ["suppressed"]


class _Guard:
    def __init__(self, why):
        self.why, self.seen = why, []

    def check(self, user_id, text, scope):
        self.seen.append(text)
        return self.why


def test_blocked_rule_is_dropped(world):
    r = world.run([PREF, DECISION], guard=_Guard("blocked_rule"))
    assert reasons(r) == ["blocked_rule"] and len(r.items_written) == 1     # the decision
    assert world.entries()[0]["pending_landing"] == []


def test_a_rule_the_guard_would_ask_about_is_dropped(world):
    guard = _Guard("needs_confirm")
    r = world.run([PREF], guard=guard)
    assert reasons(r) == ["needs_confirm"] and guard.seen == ["数学题先解释符号"]


def _old_decision(world, iid, *, scope_type="global", scope_id=None, kind="decision"):
    world.mem.put_item(Item(item_id=iid, user_id="u1", kind=kind, content="旧的 atomic 决定",
                            provenance="explicit_history",
                            evidence=(EvidenceRef(message_id="old", session_id="s0", role="user",
                                                  quote="旧的"),),
                            scope_type=scope_type, scope_id=scope_id, session_id="s0",
                            valid_from=T - timedelta(days=3), created_at=T - timedelta(days=3),
                            status="active"))


def test_supersedes_only_same_kind_and_scope(world):
    _old_decision(world, "same")
    _old_decision(world, "other_scope", scope_type="page", scope_id="p9")
    _old_decision(world, "a_question", kind="open_question")
    r = world.run([{**DECISION, "supersedes": "same"},
                   {**cand("decision", "第二条", ev("a1", "assistant"),
                           ev("m2", quote="可以，就这样")), "supersedes": "other_scope"},
                   {**cand("decision", "第三条", ev("a1", "assistant"),
                           ev("m2", quote="可以，就这样")), "resolves": "a_question"}])
    assert len(r.items_written) == 3
    status = {i.item_id: (i.status, i.superseded_by) for i in world.mem.items("u1")}
    assert status["same"] == ("superseded", r.items_written[0].item_id)
    assert status["other_scope"] == ("active", None) and status["a_question"] == ("active", None)
    hist = world.mem.get_item("u1", "same").status_history
    assert [h["status"] for h in hist] == ["active", "superseded"] and hist[-1]["by"] == "consolidation"


def test_same_range_is_never_consolidated_twice(world):
    calls = []
    world.run([DECISION], seen=calls)
    r = world.run([DECISION], seen=calls)
    assert r.items_written == [] and r.call is None and len(calls) == 1
    assert len(world.mem.items("u1")) == 1 and len(world.entries()) == 1


def test_recall_text_is_never_consolidation_input(world):
    seen = []
    world.run([], seen=seen)
    [req] = seen
    assert req.purpose == CONSOLIDATE_PURPOSE
    assert "<历史资料>" not in req.prompt and "以下是历史资料" not in req.prompt
    assert "[m2] 用户：可以，就这样" in req.prompt and "[a1] 助手：" in req.prompt


class FakeLander:
    def __init__(self, out):
        self.out, self.keys = out, []

    def land(self, user_id, candidate, *, session_id, provenance="explicit_history",
             idempotency_key, same_as=None, page_id=None):
        self.keys.append(idempotency_key)
        return self.out


PREF = cand("preference", "数学题先解释符号", ev("m3", quote="以后数学题都先解释符号"))


def test_preference_is_kept_for_retry_not_dropped(world):
    r = world.run([PREF], lander=NullPreferenceLander())
    [entry] = world.entries()
    assert entry["pending_landing"] and not r.dropped and r.items_written == []
    key = entry["pending_landing"][0]["candidate_key"]
    assert key == candidate_key("preference", ["s1/m3"], entry["pending_landing"][0]["candidate"]["content"])
    assert retry_pending_landings(world.mem, world.sessions, FakeLander(Deferred("busy")), "u1",
                                  T, lambda p: f"{p}r") == 0
    ok = FakeLander(Landed(pending_action_id="act1"))
    assert retry_pending_landings(world.mem, world.sessions, ok, "u1", T,
                                  lambda p: f"{p}r1") == 1
    assert ok.keys == [key]
    [it] = world.mem.items("u1", kinds=["preference"])
    assert it.pending_action_id == "act1" and it.status is None
    assert retry_pending_landings(world.mem, world.sessions, ok, "u1", T,
                                  lambda p: f"{p}r2") == 0      # never twice
    assert world.entries()[-1] == {"landed": [key], "refused": []}


def test_refused_preference_is_settled(world):
    world.run([PREF])
    assert retry_pending_landings(world.mem, world.sessions, FakeLander(Refused("rule")), "u1",
                                  T, lambda p: f"{p}r") == 0
    assert world.entries()[-1]["refused"][0][1] == "rule"
    lander = FakeLander(Landed())
    assert retry_pending_landings(world.mem, world.sessions, lander, "u1", T,
                                  lambda p: f"{p}r3") == 0 and lander.keys == []


def test_landed_preference_at_consolidation_is_a_pointer(world):
    r = world.run([PREF], lander=FakeLander(Landed(spec_item_id="spec9")))
    [it] = r.items_written
    assert it.kind == "preference" and it.spec_item_id == "spec9" and it.status is None


def test_bad_reply_is_value_error(world, tmp_path):
    state = fold(world.sessions.info("s1"), world.sessions.entries("s1"))
    gw = Gateway(CFG, CallLog(tmp_path / "bad.jsonl"), mode="record",
                 providers={"scripted": ScriptedProvider("scripted", lambda m, r: "not json")})
    with pytest.raises(ValueError):
        consolidate(state, sessions=world.sessions, store=world.mem, gateway=gw, account=None,
                    lander=NullPreferenceLander(), guard=NullRuleGuard(), first=0, through=5,
                    now=T, new_id=lambda p: p + "x")
    assert world.entries() == []


# ---- Postgres: the atomic commit ----

@pytest.fixture
def pg_world(pool, tmp_path):
    with pool.connection() as conn:
        for uid in ("u1", "u2"):
            create_user(conn, UserRow(user_id=uid, background="none", condition="S2",
                                      lang=Lang.ZH, created_at=T))
    w = World.__new__(World)
    w.tmp_path, w.sessions, w.mem, w.ids = tmp_path, PgSessionStore(pool), PgMemoryStore(pool), count(1)
    _session(w.sessions, "s1", "u1", TURNS)
    _session(w.sessions, "s9", "u2", [("x1", "别人说的可以就这样", "y1", "好")])
    w.pool = pool
    return w


def test_pg_lock_is_free_during_the_model_call_and_the_later_commit_writes_nothing(pg_world):
    w = pg_world
    free = []

    def reply(request):
        if not free:
            with w.pool.connection() as conn, conn.transaction():
                free.append(conn.execute("select pg_try_advisory_xact_lock("
                                         "hashtext('consolidate:' || 's1')) as ok").fetchone())
            inner = w.run([DECISION])            # another run, completing first
            assert len(inner.items_written) == 1
        return [DECISION]
    r = w.run(reply)
    assert free[0]["ok"] is True                 # no lock was held while the model answered
    assert r.items_written == [] and r.committed is False
    assert len(w.mem.items("u1")) == 1 and len(w.entries()) == 1


def test_pg_settled_key_is_refused_under_the_lock(pg_world):
    w = pg_world
    w.run([PREF])
    key = w.entries()[0]["pending_landing"][0]["candidate_key"]
    for payload in ({"landed": [key], "refused": []}, {"landed": [], "refused": [[key, "x"]]}):
        ok = w.mem.commit_consolidation(w.sessions, session_id="s1", entry_id=f"r{len(payload)}"
                                        + str(bool(payload["landed"])), payload=payload,
                                        items=(), status_changes=(), at=T)
        assert ok is bool(payload["landed"])


def test_pg_crash_before_commit_keeps_watermark(pg_world, monkeypatch):
    from evowiki.assistant import pg_store

    w = pg_world

    def boom(*a, **kw):
        raise RuntimeError("crash while committing")
    monkeypatch.setattr(pg_store, "insert_entry", boom)
    with pytest.raises(RuntimeError):
        w.run([DECISION])
    assert w.mem.items("u1") == [] and w.entries() == []
    monkeypatch.undo()
    r = w.run([DECISION])
    assert len(r.items_written) == 1 and len(w.entries()) == 1


def _episode(world, mid, answer_mid, text):
    world.mem.upsert_episode(Episode(user_id="u1", session_id="s1", message_id=mid,
                                     answer_message_id=answer_mid, trace_run_id="r", turn_index=0,
                                     page_id=None, concept_ids=(), at=T, user_text="问",
                                     answer_text=text))


def test_a_corrected_answer_stops_being_recalled_and_a_withdrawn_decision_goes(world):
    _episode(world, "m4", "a4", "下周五是 11-27")
    first = world.run([DECISION], through=2)
    [old] = first.items_written
    r = world.run({"items": [], "corrected": ["a4", "not-shown"], "withdrawn": [old.item_id]},
                  first=2, through=5)
    assert r.committed
    [ep] = [e for e in world.mem.episodes("u1") if e.message_id == "m4"]
    assert ep.answer_text == CORRECTED_ANSWER and ep.user_text == "问"
    assert world.mem.get_item("u1", old.item_id).status == "superseded"


def test_an_old_answer_he_was_reminded_of_is_shown_for_correcting_only(tmp_path):
    w = World(tmp_path)
    w.sessions.create(SessionInfo(session_id="s2", user_id="u1", page_id=None,
                                  note_concept_id=None, created_at=T))
    w.sessions.append("s2", "e1", "user", {"message_id": "n1", "text": "不对，下周五是 11-13",
                                           "selection": None}, T)
    w.sessions.append("s2", "e2", "assistant", {"message_id": "n2", "text": "好", "sources": [],
                                                "recalled": [{"id": "ep:s1/m4", "kind": "episode"}]}, T)
    _episode(w, "m4", "a4", "下周五是 11-27")
    seen = []
    w.run({"items": [], "corrected": ["a4"]}, sid="s2", through=1, seen=seen)
    assert "[a4] 下周五是 11-27" in seen[0].prompt
    [ep] = w.mem.episodes("u1")
    assert ep.answer_text == CORRECTED_ANSWER


def test_what_the_turns_remember_tool_wrote_gets_no_card():
    from datetime import UTC, datetime

    from evowiki.assistant.session import Entry
    from evowiki.memory.consolidate import EvidenceIn, _already_landed, _Ev

    at = datetime(2026, 11, 4, tzinfo=UTC)
    entries = [Entry(entry_id="e1", session_id="s", seq=1, type="user", at=at, payload={"message_id": "m1", "text": "x"}),
               Entry(entry_id="e2", session_id="s", seq=2, type="tool", at=at,
                     payload={"name": "remember_about_me", "ok": True, "args": {"text": "大三的计算机专业学生"}})]
    ev = [_Ev(EvidenceRef(message_id="m1", session_id="s", role="user", quote="x"), at, 1, True)]

    def cand(kind, content):
        return Candidate(kind=kind, content=content, topic=None, scope_type="global",
                         evidence=[EvidenceIn(message_id="m1", role="user", quote="x")])

    assert _already_landed(cand("about_me", "计算机专业大三学生"), ev, entries)     # another order
    assert not _already_landed(cand("about_me", "每周跑三次步"), ev, entries)        # the message's other fact
    assert not _already_landed(cand("preference", "计算机专业大三学生"), ev, entries)


@pytest.mark.parametrize("forget_during_guard", [False, True])
def test_pending_retry_respects_fact_forget_until_undo(pool, tmp_path, forget_during_guard):
    from evowiki.bot.actions import ActionLedger
    from evowiki.memory.forget_fact import KIND, MemoryForgetExecutor
    from evowiki.memory.lander import S3PreferenceLander
    from evowiki.wikiapp.preferences import land_preference
    from tests.assistant.test_stated_preferences import prefs_env

    env = prefs_env(pool)
    w = World.__new__(World)
    w.tmp_path, w.sessions, w.mem, w.ids = tmp_path, PgSessionStore(pool), PgMemoryStore(pool), count(1)
    _session(w.sessions, "s1", "u1", TURNS)
    if not forget_during_guard:
        w.run([PREF])
    w.mem.upsert_episode(Episode(user_id="u1", session_id="s1", message_id="m3",
        answer_message_id="a3", trace_run_id="test", turn_index=2, page_id="p1", at=T,
        user_text=TURNS[2][1], answer_text=TURNS[2][3]))
    ledger = ActionLedger(pool, lambda: T, lambda p: p + "forget", {})
    ledger.register(KIND, MemoryForgetExecutor(pool, w.mem, ledger, lambda: T))
    row = ledger.propose("u1", kind=KIND, scope="suggestion", source="assistant",
        summary="忘掉符号", why="requested", content={"words": ["符号"], "quote": "忘掉符号"},
        key="forget", level="ask")
    lander = S3PreferenceLander(land_preference, env.repo, lambda: T, env.ids)
    if forget_during_guard:
        class ForgetGuard:
            def check(self, user_id, content, scope):
                ledger.approve("u1", row.action_id)
        w.run([PREF], lander=lander, guard=ForgetGuard())
    else:
        ledger.approve("u1", row.action_id)
    def retry():
        return retry_pending_landings(w.mem, w.sessions, lander, "u1", T,
                                      lambda p: f"{p}retry{next(w.ids)}")
    assert retry() == 0
    assert not [r for r in env.rows() if r.kind == "rule"]
    assert not w.mem.items("u1")
    ledger.undo("u1", row.action_id)
    assert retry() == 1
    assert retry() == 0
    assert len([r for r in env.rows() if r.kind == "rule"]) == 1
    assert len(w.mem.items("u1")) == 1


def test_overlapping_fact_forgets_keep_waiting_rule_rejected(pool, tmp_path):
    from evowiki.bot.actions import ActionLedger
    from evowiki.memory.forget_fact import KIND, MemoryForgetExecutor
    from evowiki.memory.lander import S3PreferenceLander
    from evowiki.wikiapp.preferences import land_preference
    from tests.assistant.test_stated_preferences import prefs_env

    env = prefs_env(pool)
    w = World.__new__(World)
    w.tmp_path, w.sessions, w.mem, w.ids = tmp_path, PgSessionStore(pool), PgMemoryStore(pool), count(1)
    _session(w.sessions, "s1", "u1", TURNS)
    w.run([PREF], lander=S3PreferenceLander(land_preference, env.repo, lambda: T, env.ids))
    [card] = [r for r in env.rows() if r.kind == "rule"]
    ledger = ActionLedger(pool, lambda: T, lambda p: f"{p}{next(w.ids)}", {})
    ledger.register(KIND, MemoryForgetExecutor(pool, w.mem, ledger, lambda: T))
    acts = []
    for word in ("符号", "数学"):
        row = ledger.propose("u1", kind=KIND, scope="suggestion", source="assistant",
            summary="忘掉", why="requested", content={"words": [word], "quote": "忘掉"},
            key=f"forget-{word}", level="ask")
        ledger.approve("u1", row.action_id)
        acts.append(row.action_id)
    ledger.undo("u1", acts[0])
    assert ledger.get("u1", card.action_id).status == "rejected"
    ledger.undo("u1", acts[1])
    assert ledger.get("u1", card.action_id).status == "proposed"
