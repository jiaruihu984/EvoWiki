"""assistant.turn (assistant@v4, src/evowiki/assistant/loop.py) and assistant.compact (compact@v7,
src/evowiki/assistant/compact.py) offline cases, fakes only (ScriptedChatProvider, the fakes of
tests/assistant). Information flow (the product specification):
neither purpose creates nodes, asks quiz questions or writes pages, so the general rules do not
apply; the purpose-specific lists below are what the code does (verified here).

assistant.turn — one chat call per model step, with tools.
  must know: the question (last user message); the selected text and its section heading; the
    section body around the selection; the user's K on the page's concepts; the user's own
    organizing principles (guidelines); digests of related pages of the SAME user (current page
    excluded); the summary of compacted turns and the kept earlier turns (question + answer text
    only); the domain-free teaching-standard excerpts (spoken_first, language, a terms_first
    excerpt); the language rule; the tool definitions of this turn (name, description, schema);
    the study-mode rules (answer first, at most one closing probe question, never only questions)
    and the five-item pedagogy list; when the selection quotes a check the learner got wrong
    (the 看不懂 quote: 题目 / 我选的 / 正确答案), the Bridge order (BRIDGE: name the error → one
    remedy → answer), and never otherwise.
  must not know: another user's pages / K / guidelines (every AssistantData read is keyed by the
    session user, tools are scoped by ctx.user_id); tool results of earlier turns; errored turns;
    quiz/check answer keys (no port carries checks: AssistantData exposes pages, K text,
    guidelines, prereqs, language only).
  Note: the current page id and title are NOT rendered in the system prompt; the model reaches
    the page through read_page (page_id omitted = the current page, resolved from ToolContext).
  harness: a tool call with bad args comes back as an is_error tool result and the turn still
    answers; a provider that returns unusable output ends the turn with a retryable
    llm_unavailable error, the question stays logged and is still observed once.

assistant.compact — one structured JSON call with thinking off (CompactReply: handoff + learning).
  must know: the previous summary (to merge; the new summary replaces it) and the answered turns
    being folded (user text with its message id + answer, plus the accepted page edits of that
    turn); the field meanings, the evidence rule (learning items cite a folded user message id).
  must not know: the kept (most recent) turns, errored turns, the selection, page text, K,
    guidelines, tools, rejected or undecided proposals. Raw turns stay in the session log.
  code, not the model: learning items whose evidence_id is not a folded user message (or a
    checked item of the previous summary) are dropped; 改过哪里 comes from accepted proposals;
    a closing summary of a lone greeting is not made (worth_summarising).
  harness: a reply that does not fit the schema, a blank reply, a refusal or a transient provider
    failure skips compaction (nothing stored, turns stay, the turn still answers, retried next
    turn; over the hard cap the oldest turns are dropped with a visible notice); a rejected /
    budget failure fails the turn."""

import json

import pytest

from evowiki.agent.compact import CompactPolicy
from evowiki.agent.tools import ActionCard, ActionItem, ToolResult, ToolSpec
from evowiki.assistant.compact import (
    COMPACT_PURPOSE,
    COMPACT_SYSTEM,
    DEFAULT_POLICY,
    compact,
    history_tokens,
    plan_cut,
)
from evowiki.assistant.contracts import MessageIn, Selection
from evowiki.assistant.loop import (
    PROMPT_VERSION,
    PURPOSE,
    SYSTEM,
    TurnDeps,
    quotes_wrong_answer,
    run_turn,
)
from evowiki.assistant.session import MemorySessionStore, SessionState, Turn, fold
from evowiki.assistant.summary import Evidence, Handoff, Learning, Recap, render_handoff, reply_json
from evowiki.assistant.tools import build_tools
from evowiki.belief.aspects import AspectConfig, AspectK, AspectKView, DefineConcept
from evowiki.goals.contracts import Aspect
from evowiki.goals.language import language_rule
from evowiki.llm.chat import ChatTurn, ToolCall
from evowiki.llm.gateway import Gateway, ProviderOutputError
from evowiki.llm.log import CallLog
from evowiki.llm.prompt_steps import strip_marks
from evowiki.llm.providers import ProviderEmptyOutputError, ProviderTransientError
from evowiki.llm.scripted import ScriptedChatProvider
from evowiki.teach.standard import section
from evowiki.wikiapp.page_reader import InMemoryPageStore
from tests.assistant.fakes import T0, Data, FakeTool, Ids
from tests.assistant.test_loop import CFG, INFO, call, setup
from tests.assistant.tools.conftest import make_page

assert PURPOSE == "assistant.turn" and COMPACT_PURPOSE == "assistant.compact"


def _bridge() -> str:
    """The wrong-answer Bridge order: stage 3 moved it from assistant.loop into the assess Skill's
    `turn_rule` reference (src/evowiki/skills/assess/references/wrong-answer.md)."""
    from evowiki.assistant.skills import library
    s = library().get("assess")
    assert s is not None and s.turn_rule is not None
    return s.refs[s.turn_rule].body


BRIDGE = _bridge()

SEL = Selection(text="用均值和标准差做单位", heading="公式")


class RecordingData(Data):
    """tests.assistant.fakes.Data that records which user every read was for."""

    def __init__(self):
        super().__init__()
        self.users: list[str] = []

    def __getattribute__(self, name):
        attr = super().__getattribute__(name)
        if name in ("page", "pages", "k_text", "guidelines", "language", "prereqs"):
            def wrapped(user_id, *a, **k):
                self.users.append(user_id)
                return attr(user_id, *a, **k)
            return wrapped
        return attr


def _k() -> AspectKView:
    k = AspectK(AspectConfig(prior_a=0.6, prior_b=1.4))
    k.apply(DefineConcept(event_id="d1", ts=T0, concept_id="zscore",
                          aspects=(Aspect.PURPOSE, Aspect.PROCEDURE)))
    return AspectKView(k, T0)


def real_tools():
    store = InMemoryPageStore({"u1": [make_page("p1")], "u2": [make_page("p9", "别人的页")]})
    tools = build_tools(pages=store, kview=_k(), search=None)
    return list(tools.values())


# --- assistant.turn: what the call must and must not know -------------------------------------

def test_turn_message_has_selection_paragraph_k_principles_pages_and_system_the_standard(tmp_path):
    # assistant@v5: the per-turn context pack is the turn's user message; the system prompt holds
    # only what stays the same for the session (prompt caching)
    deps, prov, _ = setup(tmp_path, [ChatTurn(text="答")])
    deps.data = RecordingData()
    list(run_turn(deps, INFO, MessageIn(text="这是啥", selection=SEL)))
    req = prov.chat_calls[0]
    assert req.purpose == "assistant.turn"
    s, msg = req.system, req.messages[-1]
    asked = "\n".join([*(p.text for p in msg.parts), msg.text])
    for must in ("用均值和标准差做单位", "（所在小节：公式）", "z = (x − μ)/σ",      # selection + paragraph
                 "z_score.interpret 解读结果：不会",                              # K
                 "1. 先给公式再讲直觉",                                           # principles
                 "[均值](#p:p2)"):                                                # related page
        assert must in asked and must not in s, must
    assert language_rule("zh") in s
    assert section("spoken_first") not in s and section("language") not in s   # assistant@v55
    assert s.startswith(strip_marks(SYSTEM))
    assert len(req.messages) == 1 and msg.text.endswith("他问：\n这是啥")
    assert {t.name for t in req.tools} == {"read_k", "web_search", "propose_page_edit", "ask_user"}
    assert all(t.description and t.parameters["type"] == "object" for t in req.tools)


def test_turn_reads_only_the_session_users_data(tmp_path):
    deps, prov, _ = setup(tmp_path, [ChatTurn(text="答")])
    deps.data = RecordingData()
    list(run_turn(deps, INFO, MessageIn(text="这是啥", selection=SEL)))
    assert deps.data.users and set(deps.data.users) == {"u1"}
    s = prov.chat_calls[0].system + prov.chat_calls[0].messages[-1].text
    assert "(#p:p1)" not in s                       # the current page is not in its own digest list
    assert "page_id" not in s and "p1" not in s     # the page id is not rendered (tools resolve it)


def test_turn_without_selection_has_no_selection_or_paragraph(tmp_path):
    deps, prov, _ = setup(tmp_path, [ChatTurn(text="答")])
    list(run_turn(deps, INFO, MessageIn(text="这一页讲什么")))
    s = prov.chat_calls[0].system + prov.chat_calls[0].messages[-1].text
    assert "他选中的文字" not in s and "选中文字所在的段" not in s
    assert "z = (x − μ)/σ" not in s


def test_turn_history_carries_text_not_tool_results_nor_errored_turns(tmp_path):
    deps, prov, _ = setup(tmp_path, [ChatTurn(tool_calls=(call("read_k"),)), ChatTurn(text="答一"),
                                      ChatTurn(text="答三")])
    list(run_turn(deps, INFO, MessageIn(text="问一")))
    deps.store.append("s1", "x1", "user", {"message_id": "mx", "text": "失败的问", "selection": None}, T0)
    deps.store.append("s1", "x2", "turn_error", {"message_id": "mx", "code": "internal",
                                                 "detail": "x"}, T0)
    list(run_turn(deps, INFO, MessageIn(text="问三")))
    last = prov.chat_calls[-1]
    texts = [m.text for m in last.messages]
    assert texts[:2] == ["问一", "答一"] and len(texts) == 3
    assert texts[2].endswith("他问：\n问三")
    assert "read_k 的结果" not in last.system and "失败的问" not in last.system
    assert not any("失败的问" in t or "read_k 的结果" in t for t in texts)


def test_real_read_page_reaches_the_current_page_but_never_another_users(tmp_path):
    script = [ChatTurn(tool_calls=(ToolCall(id="c1", name="read_page", args={}),
                                   ToolCall(id="c2", name="read_page", args={"page_id": "p9"}))),
              ChatTurn(text="答")]
    deps, prov, _ = setup(tmp_path, script, ts=real_tools())
    ev = list(run_turn(deps, INFO, MessageIn(text="这一页讲什么")))
    assert ev[-1].type == "done"
    tool_msgs = [m for m in prov.chat_calls[1].messages if m.role == "tool"]
    assert "page_id=p1" in tool_msgs[0].text and not tool_msgs[0].is_error
    assert tool_msgs[1].is_error and "找不到页面 p9" in tool_msgs[1].text
    assert "别人的页" not in tool_msgs[1].text


# --- assistant.turn: harness ------------------------------------------------------------------

def test_tool_call_with_bad_args_is_fed_back_as_an_error_and_the_turn_answers(tmp_path):
    script = [ChatTurn(tool_calls=(ToolCall(id="c1", name="read_page",
                                            args={"page_id": 5, "bogus": True}),)),
              ChatTurn(text="换个方式答")]
    deps, prov, _ = setup(tmp_path, script, ts=real_tools())
    ev = list(run_turn(deps, INFO, MessageIn(text="问")))
    assert ev[-1].type == "done" and ev[-1].tool_calls == 1
    back = prov.chat_calls[1].messages[-1]
    assert back.role == "tool" and back.is_error and "参数不合法" in back.text
    logged = [e.payload for e in deps.store.entries("s1") if e.type == "tool"]
    assert logged[0]["ok"] is False


def test_unusable_provider_output_is_a_retryable_error(tmp_path):
    outs = iter([ProviderEmptyOutputError("empty")])

    def chat(m, r):
        raise next(outs)

    prov = ScriptedChatProvider("scripted", chat)
    gw = Gateway(CFG, CallLog(tmp_path / "c.jsonl"), mode="record", providers={"scripted": prov},
                 record_max_attempts=1)
    store = MemorySessionStore()
    store.create(INFO)
    deps = TurnDeps(gateway=gw, tools=[], store=store, data=Data(), new_id=Ids(),
                    clock=lambda: T0)
    ev = list(run_turn(deps, INFO, MessageIn(text="问")))
    assert ev[-1].type == "error" and ev[-1].code == "llm_unavailable" and ev[-1].retryable
    kinds = [e.type for e in store.entries("s1")]
    assert kinds[0] == "user" and "turn_error" in kinds and "assistant" not in kinds


# --- assistant.compact -------------------------------------------------------------------------

def _reply(summary, unclear=(), understood=(), pending=(), requests=(), decisions=()):
    """A compact@v7 reply as the model writes it."""
    return json.dumps({
        "handoff": {"summary": summary, "decisions": list(decisions),
                    "active_requests": list(requests),
                    },
        "learning": {"unclear": [{"concept": c, "evidence_id": i} for c, i in unclear],
                     "understood": [{"concept": c, "evidence_id": i} for c, i in understood]}},
        ensure_ascii=False)


def _state(summary=""):
    turns = (Turn(message_id="m0", user_text="z 分数是什么", selection={"text": "选中的秘密"},
                  answer="是离均值几个标准差"),
             Turn(message_id="m1", user_text="出错的问", selection=None, answer=None),
             Turn(message_id="m2", user_text="那负数呢", selection=None, answer="在均值以下"),
             Turn(message_id="m3", user_text="最近的问", selection=None, answer="最近的答"))
    return SessionState(info=INFO, turns=turns, summary=summary, first_kept=0, messages=(),
                        decisions={})


def _gw(tmp_path, chat=None, complete=None):
    def no_chat(m, r):
        raise AssertionError(f"unscripted chat call {r.purpose}")
    prov = ScriptedChatProvider("scripted", chat or no_chat, complete)
    return prov, Gateway(CFG, CallLog(tmp_path / "c.jsonl"), mode="record",
                         providers={"scripted": prov}, record_max_attempts=1)


def _cgw(tmp_path, reply):
    """A gateway whose compaction call answers `reply` (a str, or a callable of the request)."""
    return _gw(tmp_path, complete=reply if callable(reply) else (lambda m, r: reply))


def test_compact_sees_old_summary_and_folded_turns_only(tmp_path):
    prov, gw = _cgw(tmp_path, _reply("聊了 z 分数和负数"))
    payload, _ = compact(_state(summary="更早：均值"), gw, None, cut=3)
    assert payload == {"version": "compact@v7",
                       "handoff": {"summary": "聊了 z 分数和负数", "decisions": [],
                                   "active_requests": []},
                       "learning": {"unclear": [], "understood": []}, "edits": [],
                       "first_kept": "m3"}
    [req] = prov.calls
    assert req.purpose == "assistant.compact" and req.prompt_version == "compact@v16"
    p = req.prompt
    for must in ("[m0] 用户：z 分数是什么", "助手：是离均值几个标准差", "[m2] 用户：那负数呢"):
        assert must in p, must
    for never in ("最近的问", "最近的答", "出错的问", "选中的秘密"):
        assert never not in p and never not in req.system, never
    assert "z = (x" not in req.system and "先给公式再讲直觉" not in req.system


def test_compact_blank_or_non_json_reply_never_becomes_a_summary(tmp_path):
    for reply in ("   ", "问了什么：\n- z 分数", '{"handoff": {"summary": "x"}, "questions": []}'):
        _, gw = _cgw(tmp_path / str(len(reply)), reply)
        with pytest.raises(ValueError, match="unusable"):
            compact(_state(), gw, None, cut=3)


@pytest.mark.parametrize("exc", [ProviderEmptyOutputError("cut off", status="truncated"),
                                 ProviderEmptyOutputError("empty")])
def test_compact_provider_no_text_reply_is_a_value_error(tmp_path, exc):
    def raise_(m, r):
        raise exc
    prov, gw = _cgw(tmp_path, raise_)
    with pytest.raises(ValueError, match="no summary") as info:
        compact(_state(), gw, None, cut=3)
    assert isinstance(info.value.__cause__, ProviderOutputError)
    # thinking is off, so the gateway's retry of a starved reply (thinking off) has nothing to
    # try: one paid call, never the extra one the 2026-11-03 live run paid for
    assert len(prov.calls) == 1


TINY = CompactPolicy(history_budget=50, keep_turns=1, keep_budget=50, hard_cap=10_000,
                     summary_max_tokens=100)


def _compacting_deps(tmp_path, compact_reply, policy=TINY):
    """`compact_reply(request)` answers the compaction call (the raw JSON text)."""
    prov, gw = _gw(tmp_path, lambda m, r: ChatTurn(text="答" + "字" * 300),
                   lambda m, r: compact_reply(r))
    store = MemorySessionStore()
    store.create(INFO)
    deps = TurnDeps(gateway=gw, tools=[], store=store, data=Data(), new_id=Ids(), clock=lambda: T0, compaction=policy)
    return deps, prov, store


def test_blank_compaction_in_a_turn_is_skipped_and_the_turn_answers(tmp_path):
    deps, prov, store = _compacting_deps(tmp_path, lambda r: "")
    for i in range(3):
        assert list(run_turn(deps, INFO, MessageIn(text=f"问{i}")))[-1].type == "done"
    assert any(r.purpose == COMPACT_PURPOSE for r in prov.calls)
    assert not any(e.type == "compaction" for e in store.entries("s1"))


def test_compaction_provider_failure_skips_compaction_and_the_turn_still_answers(tmp_path):
    state = {"fail": False}

    def reply(r):
        if state["fail"]:            # a real provider failure, not a blank reply
            raise ProviderTransientError("timeout")
        return _reply("摘要")

    deps, _, store = _compacting_deps(tmp_path, reply)
    state["fail"] = True
    for i in range(2):
        list(run_turn(deps, INFO, MessageIn(text=f"问{i}")))
    ev = list(run_turn(deps, INFO, MessageIn(text="问2")))
    assert ev[-1].type == "done"
    assert not any(e.type == "compaction" for e in store.entries("s1"))    # nothing stored
    assert len(fold(INFO, store.entries("s1")).turns) == 3                 # turns kept


# --- a session long enough that compaction really triggers (shared with the live case) ---------

# Ten realistic z-score turns (question, answer). Under DEFAULT_POLICY this short session (~2100
# estimated tokens) fits and is not compacted; under LONG_POLICY (explicit, small) it is: the fold
# is the first six turns (>= 1500 characters), the open question (comparing scores across
# differently shaped distributions) is raised in m2 and never resolved later.
LONG_POLICY = CompactPolicy(history_budget=1500, keep_turns=4, keep_budget=1000, hard_cap=2500,
                            summary_max_tokens=500)
LONG_TURNS = (
    ("z 分数到底是什么？课本上写 z = (x − μ)/σ，符号我认识，可是不知道它想表达什么，为什么要先减再除？",
     ("可以把它看成换单位。先减去平均值 μ，看这个数比平均高还是低、差了多少分；再除以标准差 σ，"
     "把“差了多少分”换成“差了几个标准差”。这样不管原来的分数是百分制还是一百五十分制，"
     "换算后都在同一把尺子上。举个例子：全班数学平均 70 分，标准差 10 分，你考了 85 分，"
     "那么 z = (85 − 70)/10 = 1.5，意思是你比平均高出一个半标准差。z 是正的就在平均以上，"
     "负的就在平均以下，0 就正好等于平均。你可以先记住这一句：z 分数回答的是“离平均有几个标准差”。")),
    ("那标准差本身是什么？我只知道它和“分散”有关，具体怎么理解？",
     ("标准差衡量一组数离平均值通常有多远。如果全班分数都挤在 68 到 72 之间，标准差就小，"
     "比平均高 5 分已经很突出；如果分数从 30 到 100 都有，标准差就大，高 5 分就不算什么。"
     "计算上是先求每个数和平均的差，平方后取平均，再开方，开方是为了让单位回到“分”。"
     "所以 z 分数里除以 σ，相当于问：你比平均高的那段距离，在这个班里算大还是算小。"
     "同样高 15 分，在标准差 5 分的班里是 z = 3，非常少见；在标准差 30 分的班里只是 z = 0.5。")),
    (("懂了，所以 z = −1 就是比平均低一个标准差，不是低 1 分。那我拿 z 分数比较语文和数学可以吗？"
     "比如语文 z = 1.2，数学 z = 0.8，能说我语文考得更好吗？我总觉得两门的分数分布不一样，有点不放心。"),
     ("在两门的分布形状差不多（比如都大致对称、接近正态）的时候，可以这样说：语文 z = 1.2 表示你在"
     "语文里比平均高 1.2 个标准差，比数学的 0.8 更靠前。但如果其中一门的分布很偏，比如数学题太难，"
     "大多数人挤在低分、少数人拉得很高，那同样的 z 分数对应的名次就不一样了，直接比较会误导。"
     "这个“分布形状不同时还能不能比”的问题我们还没讲完，需要看百分位数或者分布图才能判断，"
     "你先记着这个疑问，后面再细说。")),
    ("z 分数能是小数吗？我看到有人算出 z = 2.37 这种。",
     ("可以，z 分数是连续的，算出来是多少就是多少。z = 2.37 表示比平均高出 2.37 个标准差。"
     "取整数只是为了好记，比如常说“超过两个标准差就很少见”。在正态分布里，大约 68% 的数落在 "
     "z = −1 到 1 之间，大约 95% 落在 −2 到 2 之间，所以 z = 2.37 在一个接近正态的班里已经是前 1% "
     "左右了。小数本身没有任何特别含义，只是更精确地说明了位置。"
     "报告成绩时一般保留一到两位小数就够了，比如 z = 2.37 或 2.4；位数再多也不会让结论更可靠，"
     "因为平均值和标准差本身就是从有限的人数里估出来的，带着误差。")),
    ("如果全班只有一个人比平均高很多，平均值会被拉高吗？这样 z 分数还准吗？",
     ("会被拉高一些，标准差也会变大。一个极端值会同时影响 μ 和 σ：平均往它那边移，"
     "标准差因为多了一段很远的距离而变大。结果是其他人的 z 分数都会被压低一点，"
     "那个极端值自己的 z 分数也不会像你想的那么夸张。所以样本很小或者有明显异常值时，"
     "z 分数要谨慎使用，有时会先检查异常值，或者改用中位数和四分位距这类不太受极端值影响的量。"
     "对一个几十人的班，一两个特别高的分数影响有限，但对只有五六个人的小组就影响很大。")),
    ("我刚才把页面上“z 分数的意义”那一节改成了用考试排名举例，你看这样讲对吗？",
     ("这样讲是对的，用排名举例很直观：z 分数越大，在班里的位置越靠前。只要补一句提醒："
     "z 分数说的是离平均有几个标准差，不是名次本身；名次要通过分布才能换算，"
     "在接近正态的时候可以用表格或计算器从 z 查到百分位。你改的那一节已经说清了正负号的含义，"
     "也保留了 z = (x − μ)/σ 的公式，读起来比原来那段纯文字的定义更容易进入。")),
    ("那反过来，已知 z 分数和平均、标准差，能求出原来的分数吗？",
     ("可以，把公式倒过来：x = μ + zσ。比如平均 70、标准差 10，z = −0.5，那么 x = 70 + (−0.5)×10 = 65 分。"
     "这在两种场景里常用：一是老师给出“比平均高一个标准差算优秀”，你想知道具体要考几分；"
     "二是把不同考试的 z 分数换回同一套分数制度，方便家长看懂。记住乘的是标准差，不是直接加减 z。"
     "你可以自己试一个：平均 80、标准差 6，z = 1.5 对应多少分？答案是 80 + 1.5×6 = 89 分。"
     "反过来算的时候最常见的错是忘了 z 的正负号，负的 z 要往平均以下减。")),
    ("如果两个班标准差不一样，同样考 80 分，z 分数会不同吧？",
     ("会不同。假设两个班平均都是 70 分，甲班标准差 5 分，乙班标准差 20 分。在甲班考 80 分，"
     "z = (80 − 70)/5 = 2，是很突出的成绩；在乙班考 80 分，z = (80 − 70)/20 = 0.5，只是略高于平均。"
     "原始分一样，但在各自班里的位置差很多，这正是 z 分数有用的地方：它把“相对位置”从原始分里分离出来。"
     "这也说明只看原始分会误判：乙班的 80 分看起来和甲班一样，其实在乙班里只是中上水平；"
     "反过来，甲班里一个 75 分的同学，z = 1，可能比乙班 85 分（z = 0.75）的同学在各自班里更靠前。")),
    ("嗯，这个我理解了。正态分布那个 68% 和 95% 是怎么来的？需要背吗？",
     ("那是正态分布曲线下面积的结果：曲线在 −1 到 1 个标准差之间的面积约占 68%，在 −2 到 2 之间约占 95%，"
     "在 −3 到 3 之间约占 99.7%，常叫“68-95-99.7 规则”。初学时记住这三个数就够用了，"
     "更细的比例查标准正态表或用计算器。注意它只对接近正态的分布成立，分布很偏的时候不能直接套。"
     "比如收入分布通常右偏，少数人收入极高，这时 z = 2 对应的比例和正态时差得很远；"
     "考试如果题目太难或太简单，分数也会偏，用这个规则估算名次就会出错。")),
    ("好的，今天先到这里。", "好，下次可以接着讲分布形状不同时怎么比较。"),
)


def long_session(user_id: str = "u1") -> SessionState:
    turns = tuple(Turn(message_id=f"m{i}", user_text=q, selection=None, answer=a)
                  for i, (q, a) in enumerate(LONG_TURNS))
    return SessionState(info=INFO.model_copy(update={"user_id": user_id}), turns=turns,
                        summary="", first_kept=0, messages=(), decisions={})


def folded_chars(state: SessionState, keep: int = 4) -> int:
    return sum(len(t.user_text) + len(t.answer or "") for t in state.turns[:len(state.turns) - keep])


def test_long_session_fits_the_default_policy_and_triggers_a_small_one_and_folds_enough():
    st = long_session()
    assert plan_cut(st, DEFAULT_POLICY) is None                   # 20k budget: nothing to do
    assert history_tokens(st) < DEFAULT_POLICY.history_budget
    assert history_tokens(st) > LONG_POLICY.history_budget and len(st.turns) > LONG_POLICY.keep_turns
    assert plan_cut(st, LONG_POLICY) == 6
    assert folded_chars(st) >= 1500
    # the 2026-10-14 live summary of a three-turn fold was 274 characters: a fold this long
    # leaves room for a real summary to be shorter
    assert folded_chars(st) > 4 * 274


def test_long_session_compaction_folds_six_turns_with_the_open_question(tmp_path):
    prov, gw = _cgw(tmp_path, _reply("z 分数"))
    payload, _ = compact(long_session(), gw, None, cut=6, policy=LONG_POLICY)
    assert payload["first_kept"] == "m6"
    p = prov.calls[0].prompt
    assert "语文和数学" in p and "分布形状" in p                    # the open question is folded
    for kept in LONG_TURNS[6:]:
        assert kept[0] not in p                                   # kept turns stay verbatim


# --- assistant@v4: study-mode reply, pedagogy list, Bridge order for a quoted wrong answer -------

WRONG_QUOTE = Selection(kind="wrong_answer", text=("题目：z = −1 表示什么？\n选项：\n1) 比平均高一个标准差\n"
                              "2) 比平均低一个标准差\n我选的：比平均高一个标准差\n"
                              "正确答案：比平均低一个标准差"), heading="解读")


def test_turn_system_first_judges_what_he_is_doing_and_has_no_fixed_pattern():
    # assistant@v55: the user's core prompt replaces answer-first and the pedagogy list
    for must in ("先判断学习者是在询问知识、寻求解释、完成练习、请求提示，还是希望修改 Wiki",
                 "优先提供适当提示，不抢先公布答案", "不为了教学而强制提问",
                 "K 是对掌握情况的估计，不是绝对事实", "不套用固定句式",
                 "回答问题不等于获得修改 Wiki", "建议、待确认草稿、已执行修改和执行失败"):
        assert must in SYSTEM, must
    for gone in ("永远先给答案", "1. 主动学习", "5. 元认知", "耐心的老师"):
        assert gone not in SYSTEM, gone


def test_quoted_wrong_answer_adds_the_bridge_order(tmp_path):
    deps, prov, _ = setup(tmp_path, [ChatTurn(text="答")])
    list(run_turn(deps, INFO, MessageIn(text="看不懂，为什么错了", selection=WRONG_QUOTE)))
    system, s = prov.chat_calls[0].system, prov.chat_calls[0].messages[-1].text
    assert system.startswith(strip_marks(SYSTEM)) and BRIDGE not in system   # the order is the turn's own
    assert BRIDGE in s
    first, fix, answer = (s.index("先指出错在哪"), s.index("再选一种补救方式"),
                          s.index("最后再回答"))
    assert first < fix < answer
    for remedy in ("换一个例子", "前置知识", "对比", "分几步"):
        assert remedy in s, remedy
    assert "我选的：比平均高一个标准差" in prov.chat_calls[0].messages[-1].parts[0].text  # the quote


@pytest.mark.parametrize("sel", [None, SEL,
                                 Selection(text="回想提示：z 的符号\n我的回想：忘了"),
                                 Selection(text="题目：只是一段带“题目”字样的笔记"),
                                 WRONG_QUOTE.model_copy(update={"kind": "text"})])
def test_no_bridge_without_a_quoted_wrong_answer(tmp_path, sel):
    deps, prov, _ = setup(tmp_path, [ChatTurn(text="答")])
    list(run_turn(deps, INFO, MessageIn(text="这是啥", selection=sel)))
    s = prov.chat_calls[0].system + prov.chat_calls[0].messages[-1].text
    assert BRIDGE not in s and "先指出错在哪" not in s


def test_bridge_detection_matches_the_followup_quote_shape():
    from evowiki.wikiapp.followup import _quote
    from evowiki.wikiapp.signals import IDK_TEXT

    class Item:
        stem, options, answer = "z = −1 表示什么？", ("高一个标准差", "低一个标准差"), 1

    for chosen in ("高一个标准差", ""):                # a wrong option, and 不知道
        q = _quote(Item(), chosen, "", "解读")
        sel = Selection(kind=q.kind, text=q.text, heading=q.heading)
        assert quotes_wrong_answer(MessageIn(text="看不懂", selection=sel)), q.text
    assert IDK_TEXT in _quote(Item(), "", "", None).text
    cue = _quote(None, "忘了", "z 的符号", None)
    assert not quotes_wrong_answer(MessageIn(text="看不懂", selection=Selection(kind=cue.kind, text=cue.text)))


# --- compact@v7: handoff + checked learning, re-compaction merges, raw turns stay ---------------

def test_compact_system_names_the_schema_and_the_merge_rule():
    for key in ("handoff", "summary", "decisions", "active_requests",
                "learning", "unclear", "understood", "evidence_id"):
        assert f'"{key}"' in COMPACT_SYSTEM, key
    assert "先继承旧交接中仍有效的信息" in COMPACT_SYSTEM
    assert "本轮没提到旧知识点：原样保留" in COMPACT_SYSTEM


def _logged_state():
    """Three answered turns; turn m0 raised two proposals (one accepted, one rejected)."""
    store = MemorySessionStore()
    store.create(INFO)
    n = iter(range(1, 100))
    a = lambda t, p: store.append("s1", f"e{next(n)}", t, p, T0)
    draft = {"kind": "add_section", "page_id": "p1", "heading": "负的 z 分数", "text": "正文",
             "why": "刚问过"}
    a("user", {"message_id": "m0", "text": "z 分数是什么", "selection": None})
    a("proposal", {"proposal_id": "pr1", "message_id": "m0", "draft": draft})
    a("proposal", {"proposal_id": "pr2", "message_id": "m0",
                   "draft": {**draft, "kind": "rewrite_section", "heading": "被拒的节"}})
    a("assistant", {"message_id": "a0", "text": "是离均值几个标准差", "sources": [],
                    "proposals": ["pr1", "pr2"], "tool_calls": 2, "llm_calls": 2})
    a("user", {"message_id": "m1", "text": "那负数呢", "selection": None})
    a("assistant", {"message_id": "a1", "text": "在均值以下", "sources": [], "proposals": [],
                    "tool_calls": 0, "llm_calls": 1})
    a("decision", {"proposal_id": "pr1", "status": "accepted", "applied": ["s9"], "detail": ""})
    a("decision", {"proposal_id": "pr2", "status": "rejected", "applied": [], "detail": ""})
    a("user", {"message_id": "m2", "text": "最近的问", "selection": None})
    a("assistant", {"message_id": "a2", "text": "最近的答", "sources": [], "proposals": [],
                    "tool_calls": 0, "llm_calls": 1})
    return store


def test_compact_prompt_marks_accepted_page_edits_only(tmp_path):
    store = _logged_state()
    state = fold(INFO, store.entries("s1"))
    assert state.turns[0].edits == ("加了小节「负的 z 分数」",)
    prov, gw = _cgw(tmp_path, _reply("z 分数"))
    payload, _ = compact(state, gw, None, cut=2)
    p = prov.calls[0].prompt
    assert "（这一轮改了页面：加了小节「负的 z 分数」；系统已记下）" in p
    assert "被拒的节" not in p and "被拒的节" not in json.dumps(payload, ensure_ascii=False)
    assert payload["edits"] == ["加了小节「负的 z 分数」"]           # code's, not the model's
    assert "最近的问" not in p and "最近的答" not in p           # the verbatim tail stays out


def test_recompaction_folds_the_previous_summary_and_replaces_it_raw_turns_stay(tmp_path):
    summaries = iter([_reply("第一次摘要"), _reply("第二次摘要")])
    deps, prov, store = _compacting_deps(
        tmp_path, lambda r: next(summaries),
        # one ~300-token turn is kept: two turns are over the budget, so every turn from the third
        # on folds the one before it
        CompactPolicy(history_budget=400, keep_turns=1, keep_budget=400, hard_cap=1000,
                      summary_max_tokens=100))
    for i in range(4):
        assert list(run_turn(deps, INFO, MessageIn(text=f"问{i}")))[-1].type == "done"
    reqs = [r for r in prov.calls if r.purpose == COMPACT_PURPOSE]
    assert len(reqs) == 2
    assert all(r.prompt_version == "compact@v16" and r.system.startswith(strip_marks(COMPACT_SYSTEM))
               for r in reqs)
    assert "之前已确认的交接信息" not in reqs[0].prompt
    first = Recap(handoff=Handoff(summary="第一次摘要"))
    assert "以下是之前已确认的交接信息。新对话未明确改变的内容仍然有效。请根据新增信息更新交接，保留尚未解决的学习记录。\n" + reply_json(first) in reqs[1].prompt
    assert "问3" not in reqs[1].prompt                           # the kept tail is not summarised
    entries = store.entries("s1")
    state = fold(INFO, entries)
    assert state.recap.handoff.summary == "第二次摘要"            # the new summary replaces the old
    assert "第一次摘要" not in state.summary
    users = [e.payload["text"] for e in entries if e.type == "user"]
    assert users == ["问0", "问1", "问2", "问3"]                  # raw turns are never deleted
    assert sum(e.type == "assistant" for e in entries) == 4
    assert [e.type for e in entries].count("compaction") == 2


# --- compact@v7 regressions: what the 8 live compact@v6 outputs on the dev DB got wrong ---------
# (llm_call purpose='assistant.compact', prompt_version='compact@v6'; only outputs were stored, so
# the inputs below are rebuilt from them). The model's part is a prompt rule (checked here, and
# live in tests/llm_cases/live/test_live_assistant.py); the code's part is checked on a scripted
# reply that makes the v6 mistake in v7 form.

def _session(*turns, proposals=()):
    """A logged session: `turns` are (message id, user text, answer); `proposals` are
    (message id, heading, status or None) raised in that turn and decided (or not)."""
    store = MemorySessionStore()
    store.create(INFO)
    n = iter(range(1, 200))
    a = lambda t, p: store.append("s1", f"e{next(n)}", t, p, T0)
    for mid, q, ans in turns:
        a("user", {"message_id": mid, "text": q, "selection": None})
        for pmid, heading, status in proposals:
            if pmid == mid:
                pid = f"pr_{heading}"
                a("proposal", {"proposal_id": pid, "message_id": mid, "draft": {
                    "kind": "add_section", "page_id": "p1", "heading": heading, "text": "正文",
                    "why": "他要的"}})
                if status:
                    a("decision", {"proposal_id": pid, "status": status, "applied": [],
                                   "detail": ""})
        a("assistant", {"message_id": f"a_{mid}", "text": ans, "sources": [], "proposals": [],
                        "tool_calls": 0, "llm_calls": 1})
    return store


ZCHAT = (("m0", "z 分数是什么意思", "z 分数说一个分数离平均有几个标准差。"),
         ("m1", "考考我", "第 1 题：平均 70、标准差 10，考 85 分的 z 是多少？"),
         ("m2", "1.5", "对。第 2 题：z = -1.5 说明什么？"),
         ("m3", "好", "那你先想想第 2 题，想好了告诉我。"),
         ("m4", "撤销刚才那个", "已撤销刚才的修改。"))


def test_v6_regression_a_to_do_cited_from_the_assistant_never_becomes_unclear(tmp_path):
    """v6 put 「等用户提供两道重复的题」 and an unanswered question into 还没懂的: in v7 they are
    pending, and an unclear item that cites the assistant's own message is dropped by code."""
    st = fold(INFO, _session(*ZCHAT).entries("s1"))
    reply = _reply("出了两道题，第 1 题他答对了。",
                   unclear=[("等用户提供两道重复的题", "a_m3"), ("z = -1.5 的含义", "a_m2")],
                   understood=[("由分数求 z", "m2")],
                   pending=[("第 2 题还没答", "等他回答 z = -1.5 说明什么")])
    _, gw = _cgw(tmp_path, reply)
    payload, _ = compact(st, gw, None, cut=5)
    assert payload["learning"] == {"unclear": [],
                                   "understood": [{"concept": "由分数求 z", "evidence_id": "m2"}]}
    assert [d["evidence_id"] for d in payload["dropped"]] == ["a_m3", "a_m2"]
    assert "pending" not in payload["handoff"]
    body = strip_marks(COMPACT_SYSTEM)
    assert "还没答的题" in body and "每轮另外现读" in body


def test_v6_regression_commands_are_not_questions():
    """v6 listed 「好」「撤销」「考考我」 under 问了什么: there is no question list any more, and
    the schema refuses one."""
    assert "问了什么" not in COMPACT_SYSTEM
    with pytest.raises(ValueError):                # a stray question list does not fit the schema
        from evowiki.assistant.summary import CompactReply
        CompactReply.model_validate_json('{"handoff": {}, "learning": {}, "questions": ["好"]}')


def test_v6_regression_said_he_understood_is_not_mastery(tmp_path):
    """「这个我理解了」 lands in learning.understood with its message, rendered as a self-report;
    nothing in compaction writes K (compact takes no K port)."""
    st = fold(INFO, _session(("m0", "嗯，这个我理解了。68% 怎么来的？", "曲线下面积。")).entries("s1"))
    _, gw = _cgw(tmp_path, _reply("讲了 68-95-99.7 规则", understood=[("不同班级比较 z", "m0")]))
    payload, _ = compact(st, gw, None, cut=1)
    recap = Recap(learning=Learning.model_validate(payload["learning"]))
    assert "他说懂了或答对过（自述不等于掌握）：\n- 不同班级比较 z" in render_handoff(recap)
    assert set(payload) == {"version", "handoff", "learning", "edits", "first_kept"}


def test_v6_regression_a_draft_he_never_confirmed_is_not_an_edit(tmp_path):
    """v6 wrote 「已生成拖动配对交互草稿」 as a change: v7's edits are code's, from accepted
    proposals only; an undecided or rejected draft has no field."""
    store = _session(("m0", "帮我加个拖动配对的组件", "草稿在这，确认后加上。"),
                     ("m1", "那这一节也改一下", "也给了草稿。"),
                     proposals=(("m0", "拖动配对", None), ("m1", "被拒的节", "rejected")))
    st = fold(INFO, store.entries("s1"))
    prov, gw = _cgw(tmp_path, _reply("给了两份草稿"))
    payload, _ = compact(st, gw, None, cut=2)
    assert payload["edits"] == []
    assert "这一轮改了页面" not in prov.calls[0].prompt
    assert "「页面改了哪里」由代码记录" in strip_marks(COMPACT_SYSTEM)


def test_v6_regression_a_session_request_stays_in_the_session_only(tmp_path):
    """v6 put 「记住以后公式先解释符号」 under 讲过什么. v7 keeps it as an active request for the
    next turns (in the system prompt), marked as this session's only, and compaction writes no
    preference (long-term rules only come from the memory flow with its own checks)."""
    summaries = iter([_reply("讲了标准差和方差的区别", requests=["给公式前先解释每个符号"])])
    deps, prov, store = _compacting_deps(
        tmp_path, lambda r: next(summaries),
        CompactPolicy(history_budget=400, keep_turns=1, keep_budget=400, hard_cap=1000,
                      summary_max_tokens=200))
    for text in ("标准差和方差有什么区别？", "记住以后公式先解释符号", "那 z 呢"):
        assert list(run_turn(deps, INFO, MessageIn(text=text)))[-1].type == "done"
    last = [r for r in prov.chat_calls if r.purpose == "assistant.turn"][-1]
    assert "他在这次对话里提的要求（只在这次对话有效）：\n- 给公式前先解释每个符号" in last.system
    assert "不是他的授权" in last.system
    assert {e.type for e in store.entries("s1")} <= {"user", "assistant", "compaction"}
    assert "要求仅在本次会话有效" in strip_marks(COMPACT_SYSTEM)


def test_v6_regression_resolved_items_of_the_old_summary_are_not_carried_by_code(tmp_path):
    """The new summary replaces the old one: an unclear item the model leaves out (resolved
    since) is gone — code never unions old and new lists. A kept one may keep its old id."""
    prev = Recap(handoff=Handoff(summary="讲了 z，第 2 题还没答"),
                 learning=Learning(unclear=[Evidence(concept="负的 z", evidence_id="m0"),
                                            Evidence(concept="除以 σ", evidence_id="m1")]))
    st = fold(INFO, _session(("m0", "负的 z 不懂", "在均值以下。"), ("m1", "为啥除以 σ", "统一单位。"),
                             ("m2", "第 2 题选 B，负的 z 我懂了", "对。")).entries("s1"))
    st = st.model_copy(update={"first_kept": 2, "recap": prev, "summary": render_handoff(prev)})
    prov, gw = _cgw(tmp_path, _reply("他答对了第 2 题", unclear=[("除以 σ", "m1")],
                                     understood=[("负的 z", "m2")]))
    payload, _ = compact(st, gw, None, cut=3)
    assert payload["learning"] == {"unclear": [{"concept": "除以 σ", "evidence_id": "m1"}],
                                   "understood": [{"concept": "负的 z", "evidence_id": "m2"}]}
    assert "第 2 题还没答" in prov.calls[0].prompt          # the model saw it to drop it
    assert "本轮用户明确表示已经理解同一个问题" in COMPACT_SYSTEM


def test_v6_regression_a_lone_greeting_costs_no_summary_call(tmp_path):
    from datetime import timedelta
    from types import SimpleNamespace

    from evowiki.assistant.session import SessionInfo
    from evowiki.assistant.wrapup import InlineWrapups

    store = MemorySessionStore()
    prev = INFO.model_copy(update={"session_id": "s0", "created_at": T0 - timedelta(days=1)})
    store.create(prev)
    store.append("s0", "e1", "user", {"message_id": "m0", "text": "你好", "selection": None}, T0)
    store.append("s0", "e2", "assistant", {"message_id": "a0", "text": "你好！想聊哪一页？",
                                           "sources": [], "proposals": [], "tool_calls": 0,
                                           "llm_calls": 1}, T0)
    new = SessionInfo(session_id="s_new", user_id=INFO.user_id, page_id=INFO.page_id,
                      note_concept_id=None, created_at=T0)
    store.create(new)
    prov, gw = _cgw(tmp_path, _reply("打了招呼"))
    InlineWrapups(SimpleNamespace(store=store, gateway=gw, memory=None, compaction=DEFAULT_POLICY,
                                  data=SimpleNamespace(language=lambda uid: "zh"), new_id=Ids(),
                                  clock=lambda: T0)).opened(new)
    assert prov.calls == [] and [e.type for e in store.entries("s0")] == ["user", "assistant"]


# --- assistant@v9 (still in v14): 「太笼统，帮我拆解」 -> read the outline, then the split draft in the same turn ----

SPLIT_ASK = "这一页太笼统了，帮我拆解一下"


class ActingTool(FakeTool):
    """A FakeTool whose spec is an action tool (`acts`): the turn then offers its tools through
    Skills (stage 3), so split_page comes with the edit-wiki/path reference."""

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name=self.name, description=f"{self.name} 工具",
                        parameters={"type": "object", "properties": {}}, effect="propose", acts=True)


def split_tools():
    """Real read_page on a page of the session user + a fake read_path and split_page (the real
    split_page needs the ledger on Postgres; its draft is covered in tests/assistant/test_action_tools)."""
    card = ActionCard(action_id="a1", kind="page_split", title="把「公式」拆成 2 页",
                      items=(ActionItem(key="n1", title="甲"), ActionItem(key="n2", title="乙")),
                      confirm=True)
    read_path = FakeTool("read_path", "read", ToolResult(content="路径：1. 公式 (p1)"))
    split = ActingTool("split_page", "propose", ToolResult(content="已生成草稿，等你确认", action=card))
    store = InMemoryPageStore({"u1": [make_page("p1")]})
    return [*build_tools(pages=store, kview=_k(), search=None).values(), read_path, split], split


def test_split_ask_reads_the_outline_then_drafts_the_split_in_the_same_turn(tmp_path):
    ts, split = split_tools()
    script = [ChatTurn(tool_calls=(call("read_ref", 0, path="edit-wiki/path"),
                                   call("read_page", 1))),
              ChatTurn(tool_calls=(call("read_path", 2),)),
              ChatTurn(tool_calls=(call("split_page", 3, page="p1"),)),
              ChatTurn(text="这页塞了两件事，拆成两页更好懂。已生成草稿，等你确认；也可以按三页拆。")]
    deps, prov, _ = setup(tmp_path, script, ts=ts)
    ev = list(run_turn(deps, INFO, MessageIn(text=SPLIT_ASK)))
    kinds = [e.type for e in ev]
    assert kinds[-1:] == ["done"] and kinds.count("action") == 1
    act = next(e for e in ev if e.type == "action")
    assert (act.kind, act.status, act.confirm) == ("page_split", "proposed", True)
    assert len(split.calls) == 1 and ev[-1].tool_calls == 4
    # only the outline was read: no section_id
    reads = [e.payload for e in deps.store.entries("s1") if e.type == "tool"]
    assert all("section_id" not in (r.get("args") or {}) for r in reads)
    req = prov.chat_calls[0]
    assert req.prompt_version.startswith(f"{PROMPT_VERSION}+core")
    for must in ("拆一下", "不要先问"):                     # the general change rules (CORE_RULES)
        assert must in req.system, must
    ref = next(e.payload for e in deps.store.entries("s1")
               if e.type == "tool" and e.payload["name"] == "read_ref")
    for must in ("当轮就调用改动工具", "不要一段一段读全文"):  # the path reference's method
        assert must in ref["content"], must
