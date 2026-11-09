"""record_evidence (stage 3 T9 / T10, design B6.1 / Q8, B9 S3-5): the assistant records bounded
objective events of this message in its own turn; code bounds the cells, the quote, the count, the
strength and the kinds, and K only aggregates (no band is set, nothing is declared)."""

import pytest

from evowiki.agent.tools import ToolContext
from evowiki.assistant.contracts import MessageIn
from evowiki.assistant.loop import TurnDeps, run_turn
from evowiki.assistant.session import MemorySessionStore, SessionInfo
from evowiki.assistant.tools.evidence import MAX_EVENTS, RecordEvidenceTool
from evowiki.assistant.wiki import WikiData, WikiTools
from evowiki.belief.aspects import AddNote, Declare, Observe
from evowiki.goals.contracts import Band
from evowiki.goals.selfreport import SELF_CLAIM
from evowiki.llm.chat import ChatTurn, ToolCall
from evowiki.wikiapp.repo import MemoryRepo
from tests.assistant.test_wiki_adapter import clock, gateway
from tests.wikiapp.script import Script
from tests.wikiapp.test_goal import NOW, Ids
from tests.wikiapp.test_page import organized


@pytest.fixture
def page():
    """u1 with one organized page: c1 (procedure, interpret) and c2 (term)."""
    env = (MemoryRepo(["u1"]), Script(), Ids())
    pid = organized(env)
    return env[0], pid, env[2]


def ctx(pid, text, *, mid="m1", note=None):
    return ToolContext(user_id="u1", session_id="s1", message_id=mid, page_id=pid,
                       note_concept_id=note, current_words=(text,))


def ev(cell, kind="ask", quote="是什么意思", **kw):
    out = {"cell": cell, "kind": kind, "quote": quote, **kw}
    if kind != "self" and "polarity" not in kw:
        out["polarity"] = "-"
    return out


def recorded(repo, prefix="assistant:"):
    return [e for e in repo.aspect_log("u1")
            if isinstance(e, Observe) and e.observation.evidence_ref.startswith(prefix)]


def test_one_event_lands_on_its_cell_with_a_code_label_and_no_band_set(page):
    repo, pid, ids = page
    tool = RecordEvidenceTool(repo, clock, ids)
    before = len(repo.aspect_log("u1"))
    r = tool.run({"events": [ev("c1.interpret")]}, ctx(pid, "z=-1 是什么意思？"))
    assert r.error is None and r.observed["observations"] == 1
    new = repo.aspect_log("u1")[before:]
    assert [type(e) for e in new] == [Observe, AddNote]           # K appended to, nothing set
    o = new[0].observation
    assert (o.cell, o.kind, o.polarity, o.evidence_ref) == ("c1.interpret", "ask", "-",
                                                             "assistant:s1:m1#0")
    assert new[1].label == "你问「是什么意思」"
    assert not any(isinstance(e, Declare) for e in repo.aspect_log("u1"))


@pytest.mark.parametrize("events, why", [
    ([ev("zz.term")], "不在这一页"),                                   # out of scope
    ([ev("c1.interpret", quote="这句他没说过")], "原话"),                 # not verbatim
    ([ev("c1.interpret", quote="是")], "原话"),                          # too short
    ([ev("c1.interpret"), ev("c1.interpret", polarity="~")], "已经记过"),  # one per (cell, kind)
])
def test_refused_events_write_nothing(page, events, why):
    repo, pid, ids = page
    tool = RecordEvidenceTool(repo, clock, ids)
    r = tool.run({"events": events}, ctx(pid, "z=-1 是什么意思？"))
    assert r.error is not None and why in r.content and recorded(repo) == []


def test_at_most_three_events_per_message_across_calls(page):
    repo, pid, ids = page
    tool = RecordEvidenceTool(repo, clock, ids)
    text = "z=-1 是什么意思？标准差我会。"
    cells = ["c1.procedure", "c1.interpret", "c2.term"]
    for i, c in enumerate(cells):
        assert tool.run({"events": [ev(c)]}, ctx(pid, text)).error is None, i
    r = tool.run({"events": [ev("c1.procedure", kind="feedback")]}, ctx(pid, text))
    assert r.error is not None and str(MAX_EVENTS) in r.content
    assert [e.observation.evidence_ref for e in recorded(repo)] == [
        "assistant:s1:m1#0", "assistant:s1:m1#1", "assistant:s1:m1#2"]
    # the schema refuses a fourth event in one call
    many = [ev(c) for c in cells] + [ev("c1.procedure", kind="feedback")]
    assert tool.run({"events": many}, ctx(pid, text, mid="m2")).error is not None


def test_the_kind_shapes_are_checked_by_the_schema(page):
    repo, pid, ids = page
    tool = RecordEvidenceTool(repo, clock, ids)
    for bad in ({"cell": "c2.term", "kind": "self", "quote": "我会"},            # no claim
                {"cell": "c2.term", "kind": "self", "quote": "我会", "polarity": "+",
                 "self_claim": "can_do"},                                        # polarity set
                {"cell": "c2.term", "kind": "ask", "quote": "我会"},             # no polarity
                {"cell": "c2.term", "kind": "ask", "quote": "我会", "polarity": "-",
                 "why": "他不擅长数学"}):                                         # free text
        assert tool.run({"events": [bad]}, ctx(pid, "我会")).error is not None
    assert recorded(repo) == []


def test_s3_5_self_report_is_typed_evidence_on_the_named_cells_only(page):
    """「我会这个」: a `self/can_do` observation per NAMED cell at the table's strength; no
    Declare, no cell becomes known, the other aspects get nothing."""
    repo, pid, ids = page
    tool = RecordEvidenceTool(repo, clock, ids)
    k0 = repo.load("u1").kview(NOW)
    aspects = [f"c1.{a.value}" for a in k0.aspects("c1")]
    r = tool.run({"events": [ev("c1.procedure", kind="self", quote="我会这个",
                                self_claim="can_do")]},
                 ctx(pid, "这个我会这个啦"))
    assert r.error is None
    obs = [e.observation for e in recorded(repo)]
    assert [(o.cell, o.kind, o.polarity, o.strength) for o in obs] == [
        ("c1.procedure", "self", *SELF_CLAIM["can_do"])]
    assert not any(isinstance(e, Declare) for e in repo.aspect_log("u1"))
    kv = repo.load("u1").kview(NOW)
    assert all(kv.cell(c).band is not Band.KNOWN for c in aspects)
    others = [c for c in aspects if c != "c1.procedure"]
    assert all(kv.cell(c) == k0.cell(c) for c in others)


def test_note_concept_session_scope(page):
    repo, _, ids = page
    tool = RecordEvidenceTool(repo, clock, ids)
    assert tool.run({"events": [ev("c1.procedure")]}, ctx(None, "是什么意思", note="c1")).error is None
    r = tool.run({"events": [ev("c2.term")]}, ctx(None, "是什么意思", mid="m2", note="c1"))
    assert r.error is not None


def test_a_turn_records_through_the_tool_with_no_other_model_call(page, tmp_path):
    repo, pid, ids = page
    gw, prov = gateway(tmp_path, [
        ChatTurn(tool_calls=(ToolCall(id="c1", name="record_evidence", args={"events": [
            ev("c1.interpret", quote="z=-1 是什么意思")]}),)),
        ChatTurn(text="比均值低一个标准差。")])
    info = SessionInfo(session_id="s1", user_id="u1", page_id=pid, note_concept_id=None,
                       created_at=NOW)
    store = MemorySessionStore()
    store.create(info)
    wiki = WikiTools(repo)
    deps = TurnDeps(gateway=gw, tools=lambda i, now: [*wiki(i, now),
                                                      RecordEvidenceTool(repo, clock, ids)],
                    store=store, data=WikiData(repo, clock), new_id=ids, clock=clock)
    out = list(run_turn(deps, info, MessageIn(text="z=-1 是什么意思？")))
    assert [e.type for e in out][-2:] == ["observed", "done"]
    assert out[-2].observations == 1
    assert len(prov.chat_calls) == 2 and prov.calls == []      # the turn's calls only
    mid = next(e.payload["message_id"] for e in store.entries("s1") if e.type == "user")
    assert [e.observation.evidence_ref for e in recorded(repo)] == [f"assistant:s1:{mid}#0"]
    assert [e.type for e in store.entries("s1")].count("observed") == 1
