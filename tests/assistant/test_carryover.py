import json
from datetime import timedelta

from evowiki.assistant.carryover import RECENT_CHARS, RECENT_TURNS, CarryOver, carry_over
from evowiki.assistant.compact import CLOSE_MIN_TOKENS
from evowiki.assistant.session import MemorySessionStore, SessionInfo
from evowiki.llm.gateway import Gateway
from evowiki.llm.log import CallLog
from evowiki.llm.models import GatewayConfig, ModelRef
from evowiki.llm.scripted import ScriptedChatProvider
from tests.assistant.fakes import T0

REF = ModelRef(provider="scripted", model="m")
GOOD = json.dumps({"handoff": {"summary": "讲了 z 分数", "decisions": [], "active_requests": []},
                   "learning": {"unclear": [{"concept": "负的 z", "evidence_id": "prev_m1"}],
                                "understood": []}}, ensure_ascii=False)
GOOD_PAYLOAD = {"version": "compact@v7",
                "handoff": {"summary": "讲了 z 分数", "decisions": [], "active_requests": []},
                "learning": {"unclear": [{"concept": "负的 z", "evidence_id": "prev_m1"}],
                             "understood": []},
                "edits": [], "first_kept": None}
LONG = "字" * CLOSE_MIN_TOKENS          # an answer long enough to be worth a closing summary


def _gw(tmp_path, text=GOOD):
    def reply(m, r):
        if isinstance(text, Exception):
            raise text
        return text(m, r) if callable(text) else text

    def no_chat(m, r):
        raise AssertionError("compaction is a JSON call")

    prov = ScriptedChatProvider("scripted", no_chat, reply)
    gw = Gateway(GatewayConfig(generator=REF, verifier=REF, allow_single_provider=True),
                 CallLog(tmp_path / "c.jsonl"), mode="record", providers={"scripted": prov})
    return prov, gw


def _session(store, sid, page, minutes, turns=1, summary=None, idle=True, user="u1",
             answer=LONG):
    """Creates a session with `turns` answered turns (the last one unanswered when not idle)
    and, if given, a compact@v7 entry (first_kept None) whose hand-over summary is `summary`."""
    info = SessionInfo(session_id=sid, user_id=user, page_id=page, note_concept_id=None,
                       created_at=T0 + timedelta(minutes=minutes))
    store.create(info)
    n = [0]

    def add(type_, payload):
        n[0] += 1
        store.append(sid, f"{sid}_e{n[0]}", type_, payload,
                     info.created_at + timedelta(seconds=n[0]))

    for i in range(turns):
        add("user", {"message_id": f"{sid}_m{i}", "text": f"问{i}", "selection": None})
        if idle or i < turns - 1:
            add("assistant", {"message_id": f"{sid}_a{i}", "text": f"答{i}{answer}", "sources": [],
                              "proposals": [], "tool_calls": 0, "llm_calls": 1})
    if summary is not None:
        add("compaction", {"version": "compact@v7", "handoff": {"summary": summary},
                           "learning": {}, "edits": [], "first_kept": None})
    return info


def test_carry_over_is_the_newest_page_summary():
    store = MemorySessionStore()
    _session(store, "a", "p1", 1, summary="旧")
    _session(store, "b", "p2", 2, summary="别的页")
    _session(store, "c", "p1", 3, summary="新")
    cur = _session(store, "cur", "p1", 10, summary="当前")
    got = carry_over(store, cur)
    assert got.page_summary == ""  # session compaction is never long-term handoff
    assert got.page_recent


def test_carry_over_skips_page_sessions_without_a_summary():
    store = MemorySessionStore()
    _session(store, "a", "p1", 1, summary="A1")
    _session(store, "bare", "p1", 3, turns=1)
    _session(store, "empty", "p1", 4, turns=1, summary="")      # an empty v7 summary
    cur = _session(store, "cur", "p1", 10, turns=0)
    assert carry_over(store, cur).page_summary == ""


def test_carry_over_is_empty_with_no_earlier_sessions():
    store = MemorySessionStore()
    cur = _session(store, "cur", "p1", 10, turns=0)
    assert carry_over(store, cur) == CarryOver(page_summary="")


def test_carry_over_gives_the_raw_words_the_summary_does_not_cover_yet():
    """The last session's closing summary is written in the background: until then its own words
    are what a new session on the page connects to."""
    store = MemorySessionStore()
    _session(store, "a", "p1", 1, summary="旧的交接")
    _session(store, "b", "p1", 3, turns=RECENT_TURNS + 1, answer="字" * (RECENT_CHARS + 10))
    got = carry_over(store, _session(store, "cur", "p1", 10, turns=0))
    assert len(got.page_recent) == RECENT_TURNS
    assert got.page_recent[0].startswith(f"他：问{RECENT_TURNS}\n你：…")     # newest first, its end
    assert got.page_summary == ""


def test_carry_over_gives_no_raw_words_once_summarised_or_from_another_page():
    store = MemorySessionStore()
    _session(store, "a", "p1", 1, turns=2, summary="交接")
    _session(store, "other", "p2", 2, turns=2)
    assert len(carry_over(store, _session(store, "cur", "p1", 10, turns=0)).page_recent) == 2
