"""Citations in a chat answer (decision 18): the writer's check, a failed one stated in place, no raw
mark on screen while streaming, a history the model reads unchanged."""

from dataclasses import replace

import pytest

from evowiki.assistant.citations import HeldSink, MemoryCitations, TurnCitations
from evowiki.assistant.contracts import MessageIn
from evowiki.assistant.loop import run_turn
from evowiki.assistant.session import fold as fold_session
from evowiki.llm.chat import ChatMessage, ChatRequest, ChatTurn, memory_fenced
from evowiki.llm.providers import ProviderTransientError
from evowiki.llm.scripted import ScriptedStreamFailure
from evowiki.sources.chat_cite import (
    ChatSource,
    check_marks,
    cited_ids,
    plain_marks,
    without_open_mark,
)
from evowiki.sources.models import SectionRow
from tests.assistant.fakes import INFO
from tests.assistant.test_streaming import MSG, Recorder, build, fold, logged

PAPER = ChatSource("src_a", "attention", 0, True)
BOOK = ChatSource("src_b", "讲义", 10, True)
NOTES = ChatSource("src_m", "笔记", None, False)
PAGE_4 = ("We compute the dot products of the query with all keys, divide each by the root of dk, "
          "and apply a softmax function to obtain the weights on the values.")
PAGES = {("src_a", 4): PAGE_4, ("src_b", 12): "特征向量是变换后方向不变的向量。"}
WHOLE = {"src_m": "注意力是一种按相关程度加权求和的办法。"}


def check(text):
    return check_marks(text, [PAPER, BOOK, NOTES], lambda s, p: PAGES.get((s, p), ""), WHOLE.__getitem__,
                       lambda _q: True, lambda _s, _p: "")


QUOTE = "divide each by the root of dk"


def test_a_quote_on_its_page_becomes_the_checked_form():
    assert check(f"要除。〔attention p.4：「{QUOTE}」〕") == f"要除。〔attention p.4｜4｜src_a：「{QUOTE}」〕"


def test_two_quotes_in_one_mark_are_two_citations_each_checked_by_itself():
    assert check(f"〔attention p.4：「{QUOTE}」「不在原文里的一句话」〕") == (
        f"〔attention p.4｜4｜src_a：「{QUOTE}」〕〔attention p.4｜未核对：「不在原文里的一句话」〕")


def test_the_shown_page_is_the_printed_one_and_the_link_keeps_the_pdf_page():
    assert check("〔讲义 p.12：「特征向量是变换后方向不变」〕") == "〔讲义 p.2｜12｜src_b：「特征向量是变换后方向不变」〕"


@pytest.mark.parametrize("wrote,stored", [
    (f"〔attention p.5：「{QUOTE}」〕", f"〔attention p.5｜未核对：「{QUOTE}」〕"),   # another page: never moved
    ("〔attention p.4：「多头注意力要除以根号 dk」〕", "〔attention p.4｜未核对：「多头注意力要除以根号 dk」〕"),   # translated
    ("〔attention p.4〕", "〔attention p.4｜未核对〕"),
    (f"〔别的论文 p.4：「{QUOTE}」〕", f"〔别的论文 p.4｜未核对：「{QUOTE}」〕"),
    (f"〔attention：「{QUOTE}」〕", f"〔attention｜未核对：「{QUOTE}」〕"),            # a paged one with no page
    ("〔attention p.4：「dk」〕", "〔attention p.4｜未核对：「dk」〕"),                 # too short to prove anything
])
def test_what_fails_stays_and_says_so(wrote, stored):
    assert check(f"前。{wrote}后。") == f"前。{stored}后。"


def test_a_material_without_pages_is_checked_whole_and_has_no_page():
    assert check("〔笔记：「按相关程度加权求和」〕") == "〔笔记｜src_m：「按相关程度加权求和」〕"
    assert check("〔笔记 p.3：「按相关程度加权求和」〕") == "〔笔记｜src_m：「按相关程度加权求和」〕"


def test_a_side_note_in_the_mark_brackets_is_plain_words():
    assert check("〔这一页没有讲平均度，这是我算的。〕") == "（这一页没有讲平均度，这是我算的。）"
    assert check("〔注〕和〔注：这是括号〕") == "（注）和（注：这是括号）"


def test_a_mark_copied_from_an_earlier_answer_gets_no_pass():
    assert check(f"〔attention p.4｜4｜src_a：「{QUOTE}」〕") == f"〔attention p.4｜4｜src_a：「{QUOTE}」〕"
    assert check("〔attention p.4｜4｜src_a：「这句原文里没有的话」〕") == "〔attention p.4｜未核对：「这句原文里没有的话」〕"
    assert check("〔attention p.4｜4〕") == "〔attention p.4｜未核对〕"                # a page's stored form


def test_checking_twice_changes_nothing():
    once = check(f"〔讲义 p.12：「特征向量是变换后方向不变」〕〔attention p.5：「{QUOTE}」〕〔attention p.4〕")
    assert check(once) == once


def test_a_stored_answer_names_its_checked_sources_and_reads_as_words():
    text = check(f"甲〔attention p.4：「{QUOTE}」〕乙〔笔记：「按相关程度加权求和」〕丙〔attention p.9〕")
    assert cited_ids(text) == {"src_a", "src_m"}
    assert plain_marks(text) == "甲（attention p.4）乙（笔记）丙（attention p.9，没有在原文核对到）"


def test_an_answer_that_broke_off_inside_a_mark_ends_before_it():
    assert without_open_mark("前面的话。〔attention p.4：「divide") == "前面的话。"
    assert without_open_mark("前面〔注") == "前面〔注"


def section(sid, text, page_from):
    return SectionRow(section_id="x", source_id=sid, parent_id=None, ord=0, depth=0, title="t",
                      page_from=page_from, page_to=None, text=text, tokens=1, summary=None,
                      read_by="text")


def reader():
    return MemoryCitations({PAPER: [section("src_a", f"第一页。〔p.4〕{PAGE_4}〔p.5〕Next page.", 1)],
                            NOTES: [section("src_m", WHOLE["src_m"], None)]})


def request(*messages):
    return ChatRequest(purpose="p", prompt_version="v", subject="s", system="sys", messages=messages)


def read_this_turn(text):
    return request(ChatMessage(role="tool", tool_call_id="c", text=text))


DENSITY = "the density is c/(n-1) on this slide"


def test_a_page_read_this_turn_counts_as_read():
    r = MemoryCitations({PAPER: [section("src_a", "〔p.4〕clean text only", 1)]},
                        reads={("src_a", 4): DENSITY})
    cite = TurnCitations(r, "u1")
    cite.saw(read_this_turn(DENSITY))
    assert cite(f"〔attention p.4：「{DENSITY}」〕") == f"〔attention p.4｜4｜src_a：「{DENSITY}」〕"


def test_a_stored_read_this_turn_did_not_read_says_so():
    r = MemoryCitations({PAPER: [section("src_a", "〔p.4〕clean text only", 1)]},
                        reads={("src_a", 4): DENSITY})
    cite = TurnCitations(r, "u1")
    assert cite(f"〔attention p.4：「{DENSITY}」〕") == f"〔attention p.4｜4｜此前读取｜src_a：「{DENSITY}」〕"


def test_memory_and_earlier_answers_are_no_reading():
    r = MemoryCitations({PAPER: [section("src_a", f"〔p.4〕{PAGE_4}", 4)]})
    cite = TurnCitations(r, "u")
    cite.saw(request(ChatMessage(role="user", origin="memory", text=QUOTE),
                     ChatMessage(role="assistant", text=QUOTE),
                     ChatMessage(role="tool", tool_call_id="c", text=memory_fenced(QUOTE)),
                     ChatMessage(role="user", text="这页写了什么")))
    assert cite(f"〔attention p.4：「{QUOTE}」〕") == f"〔attention p.4｜未核对：「{QUOTE}」〕"


def test_a_picture_page_text_layer_checks_nothing():
    paper = ChatSource("src_a", "attention", 0, True, (4,))
    r = MemoryCitations({paper: [section("src_a", f"〔p.4〕{PAGE_4}", 4)]})
    cite = TurnCitations(r, "u")
    cite.saw(read_this_turn(QUOTE))
    assert cite(f"〔attention p.4：「{QUOTE}」〕") == f"〔attention p.4｜未核对：「{QUOTE}」〕"


def test_the_shape_recalled_answers_have_is_a_citation_that_checks_nothing():
    cite = TurnCitations(reader(), "u")
    assert cite("公式在这里（attention p.13）") == "公式在这里〔attention p.13｜未核对〕"
    assert cite("别的括号（见 p.13）留着") == "别的括号（见 p.13）留着"


def test_turn_check_reads_pages_from_the_stored_marks_once_and_fails_closed():
    calls = []

    class Counting(MemoryCitations):
        def sections(self, user_id, source_id):
            calls.append(source_id)
            return super().sections(user_id, source_id)

    r = Counting({PAPER: [section("src_a", f"〔p.4〕{PAGE_4}", 4)]})
    cite = TurnCitations(r, "u")
    cite.saw(read_this_turn(QUOTE))
    assert cite(f"〔attention p.4：「{QUOTE}」〕").startswith("〔attention p.4｜4｜src_a")
    assert cite(f"〔attention p.5：「{QUOTE}」〕") == f"〔attention p.5｜未核对：「{QUOTE}」〕"
    assert calls == ["src_a"]

    class Broken(MemoryCitations):
        def sections(self, user_id, source_id):
            raise OSError("db")
    assert TurnCitations(Broken({PAPER: []}), "u")(f"〔attention p.4：「{QUOTE}」〕") == \
        f"〔attention p.4｜未核对：「{QUOTE}」〕"


def test_the_named_attachment_comes_first_among_same_titled_materials():
    twin = ChatSource("src_c", "attention", 0, True)
    r = MemoryCitations({PAPER: [section("src_a", "别的内容", 1)],
                         twin: [section("src_c", f"〔p.4〕{PAGE_4}", 4)]})
    cite = TurnCitations(r, "u", ["src_c"])
    cite.saw(read_this_turn(QUOTE))
    assert "｜src_c：" in cite(f"〔attention p.4：「{QUOTE}」〕")


# ---- streaming ----

class Out:
    def __init__(self):
        self.got = []

    def text(self, chunk):
        self.got.append(chunk)


def test_held_sink_shows_the_recalled_shape_of_a_citation_checked():
    out = Out()
    sink = HeldSink(out, TurnCitations(reader(), "u"))
    for c in "公式（attention p.13）后面（普通括号）":
        sink.text(c)
    assert "".join(out.got) == "公式〔attention p.13｜未核对〕后面（普通括号）"


@pytest.mark.parametrize("n", [1, 2, 7, 1000])
def test_held_sink_never_shows_a_raw_mark_and_ends_with_the_checked_text(n):
    answer = f"开头〔注〕要除。〔attention p.4：「{QUOTE}」〕\n另起〔不是引用\n结尾〔attention p.5〕。"
    out = Out()
    sink = HeldSink(out, check)
    for i in range(0, len(answer), n):
        sink.text(answer[i:i + n])
    assert "".join(out.got) == check(answer)
    assert not any("p.4：" in c or "p.5〕" in c for c in out.got)


def test_held_sink_lets_a_bracket_that_never_closes_go():
    out = Out()
    sink = HeldSink(out, check)
    sink.text("〔" + "长" * 2100)
    assert "".join(out.got).startswith("〔长")


def with_reader(deps):
    return replace(deps, citations=reader())


ASKED = MSG.model_copy(update={"text": f"这句在讲义里吗：{QUOTE}"})


def test_a_streamed_answer_has_no_raw_mark_and_the_commit_overwrites_nothing_shown(tmp_path):
    text = f"要除以它。〔attention p.4：「{QUOTE}」〕再看另一处。〔attention p.5：「{QUOTE}」〕"
    deps = with_reader(build(tmp_path, [ChatTurn(text=text)], chunk=3))
    live = Recorder()
    ev = list(run_turn(deps, INFO, ASKED, live=live))
    stored = logged(deps, "assistant")[0]["text"]
    assert stored == (f"要除以它。〔attention p.4｜4｜src_a：「{QUOTE}」〕"
                      f"再看另一处。〔attention p.5｜未核对：「{QUOTE}」〕")
    assert "".join(live.sinks[0]["text"]) == stored == fold(ev)
    assert not any("p.4：" in c or "p.5：" in c for c in live.sinks[0]["text"])


def test_an_answer_that_breaks_off_in_a_mark_shows_what_came_before(tmp_path):
    deps = with_reader(build(tmp_path, [ScriptedStreamFailure(
        f"要除以它。〔attention p.4：「{QUOTE}」〕后面〔attention p.5：「div",
        ProviderTransientError("cut"))]))
    ev = list(run_turn(deps, INFO, ASKED, live=Recorder()))
    assert fold(ev) == f"要除以它。〔attention p.4｜4｜src_a：「{QUOTE}」〕后面"


def test_later_turns_send_the_model_the_stored_answer_unchanged(tmp_path):
    """The prompt cache: an earlier answer is what was stored, even with the material gone by then."""
    seen = []
    deps = with_reader(build(tmp_path, [ChatTurn(text=f"甲〔attention p.4：「{QUOTE}」〕"), ChatTurn(text="乙")]))
    list(run_turn(deps, INFO, MSG))
    stored = logged(deps, "assistant")[0]["text"]
    deps = replace(deps, citations=MemoryCitations({}, {"src_a"}))
    gw = deps.gateway
    real = gw.converse

    def spy(tier, req, **kw):
        seen.append(req)
        return real(tier, req, **kw)

    gw.converse = spy
    list(run_turn(deps, INFO, MessageIn(text="再问")))
    assert [m.text for m in seen[0].messages if m.role == "assistant"] == [stored]
    assert fold_session(INFO, deps.store.entries("s1")).turns[0].answer == stored


def test_a_page_named_without_a_mark_says_it_was_not_checked():
    from evowiki.sources.chat_cite import ChatSource, bare_pages, needs_check

    one = [ChatSource("src1", "Network basics", None, True)]
    text = "讲义第 13 页给出了 ρ = 2m/(n(n-1))。另一句。"
    assert needs_check(text)
    assert bare_pages(text, one) == "讲义第 13 页给出了 ρ = 2m/(n(n-1))〔Network basics p.13｜未核对〕。另一句。"
    marked = "讲义第 13 页给出了〔Network basics p.13｜13｜src1：「rho」〕。"
    assert bare_pages(marked, one) == marked
    assert not needs_check("这一页第 3 段讲了什么")


def test_a_quote_from_a_web_page_it_read_is_no_failed_citation():
    from evowiki.sources.chat_cite import check_marks

    def nothing(*_):
        return ""

    text = "正好是 1/2。〔Percolation threshold：「bond thresholds of 1/2」〕"
    got = check_marks(text, [], nothing, nothing, lambda q: True, nothing)
    assert got == "正好是 1/2。（Percolation threshold：「bond thresholds of 1/2」）"
    got = check_marks(text, [], nothing, nothing, lambda q: False, nothing)
    assert "未核对" in got
