"""The assistant over E1's wiki storage (assistant/wiki.py): AssistantData / PageStore views of a
MemoryRepo seeded with E1's own flows, and a turn through the service applying an accepted edit.
(Question as evidence: tests/assistant/test_record_evidence.py.)"""


import pytest

from evowiki.agent.tools import ToolContext
from evowiki.assistant.contracts import MessageIn, SessionCreateIn
from evowiki.assistant.loop import TurnDeps
from evowiki.assistant.service import AssistantService
from evowiki.assistant.session import MemorySessionStore
from evowiki.assistant.wiki import (
    WikiApplier,
    WikiData,
    WikiPages,
    WikiTools,
)
from evowiki.belief.aspects import Declare
from evowiki.goals.context import page_summary_of
from evowiki.goals.observe import k_text
from evowiki.llm.chat import ChatTurn, ToolCall
from evowiki.llm.gateway import Gateway
from evowiki.llm.log import CallLog
from evowiki.llm.models import GatewayConfig, ModelRef
from evowiki.llm.scripted import ScriptedChatProvider
from evowiki.wikiapp.events import Guideline, GuidelineAdded
from evowiki.wikiapp.repo import Commit, MemoryRepo
from evowiki.wikiapp.teachspec import wiki_texts
from tests.wikiapp.script import Script
from tests.wikiapp.test_goal import NOW, Ids
from tests.wikiapp.test_organize import add_page
from tests.wikiapp.test_page import commit_wiki, organized

REF = ModelRef(provider="scripted", model="m")
CFG = GatewayConfig(generator=REF, verifier=REF, allow_single_provider=True)
ASK_REPLY = {"answer": "z=-1 表示比均值低一个标准差。", "observations": [
    {"cell": "c1.interpret", "polarity": "-", "strength": 0.6, "reason": "问 z=-1 的含义"}]}


@pytest.fixture
def env():
    return MemoryRepo(["u1"]), Script(), Ids()


@pytest.fixture
def wiki(env):
    """u1 with one organized page (c1 z 分数 → prereq c2 标准差), one unorganized page and a
    guideline, all written through E1's flows / events."""
    repo, _, ids = env
    pid = organized(env)
    raw = add_page(repo, ids, title="z 分数（未生成）")
    commit_wiki(env, GuidelineAdded(guideline=Guideline(guideline_id="g1", text="先给例子",
                                                        source="user")))
    return repo, pid, raw


def clock():
    return NOW


# ---- AssistantData / PageStore ----

def test_known_concepts_are_those_with_every_aspect_known(wiki):
    repo, _, _ = wiki
    data = WikiData(repo, clock)
    assert data.known_concepts("u1", ["c1", "zz"]) == frozenset()
    snap = repo.load("u1")
    kview = snap.kview(NOW)
    aspects = kview.aspects("c1")
    repo.commit(snap, Commit(aspect=[
        Declare(event_id=f"k_{a.value}", ts=NOW, cell=f"c1.{a.value}", value="know")
        for a in aspects]))
    assert data.known_concepts("u1", ["c1", "c2", "zz"]) == frozenset({"c1"})
    assert data.known_concepts("ghost", ["c1"]) == frozenset()


def test_page_view_digests_k_text_guidelines_and_prereqs(wiki):
    repo, pid, raw = wiki
    data = WikiData(repo, clock)
    rec = repo.load("u1").state.pages[pid]
    view = data.page("u1", pid)
    assert view.title == rec.title and view.concepts == rec.concepts == ("c1", "c2")
    texts = rec.page.texts
    assert [s.section_id for s in view.sections] == [s.id for s in rec.page.outline.sections]
    assert all(s.body == texts[s.section_id].body for s in view.sections if s.body)
    assert next(s for s in view.sections if s.section_id == "s4").body == ""  # check: no prose
    assert data.page("u1", raw).sections == ()                                  # not organized
    assert data.page("u1", "nope") is None and data.page("ghost", pid) is None

    digests = {d.page_id: d for d in data.pages("u1")}
    assert list(digests) == [pid, raw]
    assert digests[pid].summary == page_summary_of(rec.page) and digests[raw].summary == ""
    assert digests[pid].concepts == ("c1", "c2")

    snap = repo.load("u1")
    names = {c: n.name for c, n in snap.state.names.items()}
    assert data.k_text("u1", ["c1", "zz"]) == k_text(snap.kview(NOW), ["c1"], names)
    assert "z 分数" in data.k_text("u1", ["c1"])
    assert data.k_text("u1", ["zz"]) == ""
    # the teaching spec's domain-agnostic item texts: the default items, then the user's
    assert list(data.guidelines("u1"))[-1] == "先给例子"
    assert list(data.guidelines("u1")) == list(wiki_texts(snap.state, domains=True))
    # intake writes no prerequisite (relation layers): K records none for c1
    assert data.prereqs("u1", ["c1"]) == () and data.prereqs("u1", ["zz"]) == ()
    assert data.pages("ghost") == () and data.k_text("ghost", ["c1"]) == ""


def test_page_store_only_serves_organized_pages(wiki):
    repo, pid, raw = wiki
    pages = WikiPages(repo)
    wp = pages.get("u1", pid)
    assert wp is not None and wp.page == repo.load("u1").state.pages[pid].page
    assert pages.get("u1", raw) is None and pages.get("ghost", pid) is None
    assert [p.page_id for p in pages.all("u1")] == [pid]


@pytest.mark.parametrize("answer", [None, "correct", "wrong", "idk"])
def test_check_state_uses_the_same_page_snapshot_in_reads_and_context(wiki, env, monkeypatch, answer):
    from evowiki.agent.read_page import ReadPageTool
    from evowiki.assistant.loop import _page_part
    from evowiki.goals.contracts import SectionText
    from evowiki.wikiapp.events import PageSaved
    from evowiki.wikiapp.page_reader import READ_SCOPE

    repo, pid, _ = wiki
    rec = repo.load("u1").state.pages[pid]
    check = next(s for s in rec.page.outline.sections if s.check is not None)
    assert check.check.verified and check.id not in rec.page.texts
    choices = {"correct": check.check.answer, "wrong": (check.check.answer + 1) % len(check.check.options), "idk": -1}
    answers = {} if answer is None else {check.id: choices[answer]}
    # A check's structured state is authoritative, even if an old record also has prose.
    page = rec.page.model_copy(update={"texts": {**rec.page.texts, check.id: SectionText(
        section_id=check.id, body="private check text", cue="private cue",
        answer="private answer", cache_key="check")}})
    commit_wiki(env, PageSaved(record=rec.model_copy(update={"page": page, "checks": answers}), action="test"))
    snap = repo.load("u1")
    calls = 0

    def load_once(user_id):
        nonlocal calls
        calls += 1
        assert calls == 1 and user_id == "u1"
        return snap

    monkeypatch.setattr(repo, "load", load_once)
    tool = ReadPageTool(WikiPages(repo))
    result = tool.run({}, ToolContext(user_id="u1", session_id="s", page_id=pid))
    assert result.error is None and calls == 1
    line = next(line for line in result.content.splitlines() if line.startswith(f"- {check.id} ·"))
    calls = 0
    view = WikiData(repo, clock).page("u1", pid)
    assert calls == 1 and view.checks == (line,)
    context = _page_part(view).text
    assert line in context and READ_SCOPE in context and READ_SCOPE in result.content
    assert next(s for s in view.sections if s.section_id == check.id).body == ""
    assert "未写" not in line and "private" not in context and "private" not in result.content
    assert "correct_index" not in context and "correct_index" not in result.content
    assert ("未作答" in line) == (answer is None)
    assert ("答对" in line) == (answer == "correct")
    assert ("答错" in line) == (answer == "wrong")


def test_read_page_on_a_page_not_written_yet_says_what_it_covers(wiki):
    """Live S3 run 2026-10-28: a path page with no text answered 「找不到页面」 while read_path
    listed it, and the model would not split it. It reads as the page's plan instead."""
    from evowiki.agent.read_page import NOT_WRITTEN, ReadPageTool
    repo, pid, raw = wiki
    tool = ReadPageTool(WikiPages(repo))
    ctx = ToolContext(user_id="u1", session_id="s1", page_id=raw)
    out = tool.run({}, ctx)
    assert out.error is None, out.content
    assert f"page_id={raw}" in out.content and NOT_WRITTEN in out.content
    assert "要讲的：z 分数（未生成）" in out.content and "（c1）" in out.content
    assert out.sources[0].page_id == raw
    sec = tool.run({"section_id": "s1"}, ctx)
    assert sec.error is not None and "还没写" in sec.content
    assert "找不到页面 pg_nope" in tool.run({"page_id": "pg_nope"}, ctx).content
    written = tool.run({"page_id": pid}, ctx)
    assert written.error is None and NOT_WRITTEN not in written.content


# ---- a turn through the service ----

def gateway(tmp_path, script, log="c.jsonl"):
    """A record-mode gateway on its own call log (a shared log would replay earlier replies).
    `script` serves chat turns and JSON calls in order (a JSON call's entry is its raw text, e.g.
    a compaction)."""
    outs = iter(script)
    prov = ScriptedChatProvider("scripted", lambda m, r: next(outs), lambda m, r: next(outs))
    return Gateway(CFG, CallLog(tmp_path / log), mode="record",
                   providers={"scripted": prov}), prov


@pytest.mark.parametrize("literal", ["none", "page", "user", "formula"])
def test_check_id_is_guarded_but_literal_text_and_formulas_stay(wiki, env, tmp_path, literal):
    """Real fe745 reply: a structured check had no prose, so s5 missed the authority set."""
    from evowiki.goals.contracts import SectionText
    from evowiki.wikiapp.events import PageSaved

    repo, pid, _ = wiki
    rec = repo.load("u1").state.pages[pid]
    sections = tuple(s.model_copy(update={"id": "s5"}) if s.check is not None else s
                     for s in rec.page.outline.sections)
    texts = {**rec.page.texts, "s5": SectionText(section_id="s5", body="s5 private check body",
                                               cue="private cue", answer="private answer", cache_key="k5")}
    if literal == "page":
        first = next(iter(rec.page.texts))
        texts[first] = texts[first].model_copy(update={"body": "原文讨论字符串 s5。"})
    page = rec.page.model_copy(update={"outline": rec.page.outline.model_copy(update={"sections": sections}),
                                        "texts": texts})
    commit_wiki(env, PageSaved(record=rec.model_copy(update={"page": page}), action="test"))
    sentence = "我刚读了这一段（s5）的记录。" if literal != "formula" else "式子里的 $s5$ 表示一个变量。"
    script = [ChatTurn(tool_calls=(ToolCall(id="read", name="read_page", args={"section_id": "s5"}),)),
              ChatTurn(text=sentence)]
    if literal == "none":
        script.append(ChatTurn(text=sentence))  # also exercise normalization when the re-ask still leaks
    gw, provider = gateway(tmp_path, script)
    deps = TurnDeps(gateway=gw, tools=WikiTools(repo), store=MemorySessionStore(),
                    data=WikiData(repo, clock), new_id=Ids(), clock=clock)
    service = AssistantService(deps, WikiApplier(repo, clock, Ids()))
    sid = service.create_session("u1", SessionCreateIn(page_id=pid)).session_id
    question = "请解释原文里的字符串 s5。" if literal == "user" else "这道确认题有作答记录吗？"
    events = list(service.send("u1", sid, MessageIn(text=question)))
    answer = next(e.payload for e in reversed(deps.store.entries(sid)) if e.type == "assistant")
    if literal == "none":
        assert len(provider.chat_calls) == 3
        assert answer["reasked"] == [{"why": "ids", "said": "s5"}]
        assert answer["text"] == "我刚读了这一段的记录。"
        assert "s5" not in "".join(e.text for e in events if e.type == "text")
    else:
        assert len(provider.chat_calls) == 2 and "reasked" not in answer
        assert answer["text"] == sentence
    assert not any("private" in part.text for call in provider.chat_calls
                   for message in call.messages for part in message.parts if hasattr(part, "text"))


def test_service_with_wiki_ports_answers_and_applies_the_accepted_edit(wiki, tmp_path):
    repo, pid, _ = wiki
    gw, _ = gateway(tmp_path, [
        ChatTurn(tool_calls=(ToolCall(id="c1", name="propose_page_edit", args={
            "kind": "add_section", "page_id": pid, "heading": "补充：z=-1", "cue": "z=-1 表示什么？", "answer": "比均值低一个标准差。",
            "text": "z=-1 表示比均值低一个标准差。", "why": "刚问过", "paragraphs": []}),)),
        ChatTurn(text="建议加一段")])
    deps = TurnDeps(gateway=gw, tools=WikiTools(repo), store=MemorySessionStore(),
                    data=WikiData(repo, clock), new_id=Ids(), clock=clock)
    svc = AssistantService(deps, WikiApplier(repo, clock, Ids()))
    with pytest.raises(LookupError):
        svc.create_session("u1", SessionCreateIn(page_id="nope"))
    sid = svc.create_session("u1", SessionCreateIn(page_id=pid)).session_id
    ev = list(svc.send("u1", sid, MessageIn(text="z=-1 是什么意思？")))
    prop = next((e for e in ev if e.type == "proposal"), None)
    assert prop is not None, [e.model_dump() for e in ev]
    out = svc.decide("u1", prop.proposal_id, accept=True)
    assert out.status == "accepted" and len(out.applied) == 1, out
    page = repo.load("u1").state.pages[pid].page
    assert page.outline.sections[-1].id == out.applied[0]
    assert page.texts[out.applied[0]].body == "z=-1 表示比均值低一个标准差。"
