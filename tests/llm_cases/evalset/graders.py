"""Graders: pure functions of a trial's record (driver.py) and arguments. Code decides everything it
can against end state, tool ledger and paragraph diff. A judge (judge.py) is consulted only where the
args ask for it and code found nothing either way; without one the check says `undecided`, which is a
failure of the run, never a pass.

`grade(case, record, judge)` → list of `Check`; a trial passes when every check holds."""

import re
from collections.abc import Callable
from dataclasses import asdict, dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Protocol

from tests.llm_cases.evalset.spec import Case


class AskJudge(Protocol):
    def ask(self, question: str, text: str, *, language: str = "zh") -> tuple[bool, str]: ...


@dataclass(frozen=True)
class LanguageJudge:
    judge: AskJudge
    language: str

    def ask(self, question: str, text: str) -> tuple[bool, str]:
        return self.judge.ask(question, text, language=self.language)


@dataclass(frozen=True)
class Check:
    grader: str
    ok: bool
    basis: str            # the evidence, verbatim where it is text
    undecided: bool = False


# ---- what a record offers
def answers(rec: dict) -> list[str]:
    return [t["answer"] for t in rec.get("turns", ())]


def last_answer(rec: dict) -> str:
    return answers(rec)[-1] if rec.get("turns") else ""


def tool_calls(rec: dict) -> list[dict]:
    return [x for t in rec.get("turns", ()) for x in t["tools"]]


def changing_calls(rec: dict) -> list[dict]:
    """Calls that changed something or made a draft: anything in the ledger, any proposal tool.
    Memory-only quiet rows (remember_about_me) count too: the case says when they are allowed."""
    return [x for x in tool_calls(rec) if x.get("status") or x.get("effect") == "propose"]


def executed(rec: dict) -> list[dict]:
    return [x for x in tool_calls(rec) if x.get("status") == "executed"]


def pending_drafts(rec: dict) -> int:
    """Drafts raised and not decided."""
    n = sum(len(t["proposals"]) for t in rec.get("turns", ()))
    n += sum(1 for x in tool_calls(rec) if x.get("status") == "proposed")
    return n - sum(1 for d in rec.get("decisions", ()) if d.get("what") in ("proposal", "action"))


def page_after(rec: dict, pid: str) -> dict | None:
    if pid in rec["after"]["pages"]:
        return rec["after"]["pages"][pid]
    if pid in rec["after"].get("unchanged_pages", ()):
        return rec["before"]["pages"][pid]
    return None


def find_page(rec: dict, title: str) -> tuple[str, dict]:
    for k, p in rec["before"]["pages"].items():
        if p["title"] == title:
            return k, p
    raise KeyError(f"no page {title!r} before the trial")


# ---- completion claims: the failure 「已设好」 with nothing behind it
_CLAIM_VERBS = (
    "设好|设置好|设成|设为|设了|改好|改完|改成|改为|调好|调成|调整好|删好|删掉|删除|删了|移除|加好|加上|添加|存好|存下|保存|"
    "记下|记住|记好|记上|更新|安排好|排好|搭好|建好|建立(?!在)|生成|写好|写完|做好|做完|弄好|搞定|办好|处理好|完成|"
    "推迟|延后|撤销|撤回|恢复|关掉|关闭|暂停|标记|标成|记成|拆好|拆成|合并|挪到|生效")
_DONE = re.compile(rf"(?:已经?|都)[^，。！？；：\n]{{0,16}}?(?:{_CLAIM_VERBS})(?!的)|(?:{_CLAIM_VERBS})(?:好)?了(?![吗呢吧点])|"
                   r"(?:不会再|再也不会)(?:出|来|推)|现在(?:每天|生效)|从现在起")
_NOT_A_CLAIM = re.compile(
    r"没有|没能|没法|没办法|还没|并没|尚未|未(?:生效|改|设|做)|不能|不支持|无法|做不到|不会(?!再)|不了|如果|要是|的话|吗|？|\?|"
    r"可以|要不要|是否|想要|打算|建议|需要|应该|等你|确认后|确认|点一下|之后才|告诉我|你说|"
    r"草稿|待确认|准备好")
_OPEN_SENTENCE = re.compile(r"如果|要是|的话|假如|吗|？|\?|(?:^|，)[^，。]{0,5}[^以之今，。]后(?:，|不会)")     # 「暂停后，它不会再出现」: what would follow
_CLAUSE = re.compile(r"[，,：:；;（()）—]+")
_SENT = re.compile(r"[^。！？!?\n]+[。！？!?]?")
_QUOTED = re.compile(r"```.*?```|\$\$.*?\$\$|\$[^$\n]*\$|「[^」]*」|“[^”]*”|`[^`\n]*`", re.DOTALL)
_EN_DONE = re.compile(
    r"\b(?:I(?:['’]ve| have)?|we(?:['’]ve| have)?)\s+(?:already\s+)?"
    r"(?:saved|remembered|updated|changed|deleted|removed|enabled|disabled|scheduled|created|applied|set)\b"
    r"|\b(?:has|have) been (?:saved|updated|changed|deleted|applied|enabled|disabled)\b", re.IGNORECASE)
_EN_QUALIFIED = re.compile(r"\b(?:not|never|cannot|can't|couldn't|haven't|hasn't|didn't|if|would|could|draft|pending)\b", re.IGNORECASE)


def claim_sentence(text: str, *, true_of_turn: tuple[str, ...] = ()) -> str | None:
    """The first sentence stating, without a hedge, that a change happened. Independent of the
    product's own `assistant.claims` check on purpose: a grader that shares the guard's patterns
    would only ever confirm the guard. `true_of_turn`: verbs this turn's tools made true (a draft: 生成; recorded evidence: 记下)."""
    unquoted = _QUOTED.sub("", text)
    for sentence in re.split(r"(?<=[.!?])\s+|\n", unquoted):
        if _EN_DONE.search(sentence) and not _EN_QUALIFIED.search(sentence) and not sentence.rstrip().endswith("?"):
            return sentence.strip()
    for m in _SENT.finditer(unquoted):
        s = m.group(0).strip()
        if _OPEN_SENTENCE.search(s):
            continue
        for clause in _CLAUSE.split(s):       # 「已经设好了，你随时可以撤销」: the hedge is another clause's
            if _DONE.search(clause) and not _NOT_A_CLAIM.search(clause) and not any(v in clause for v in true_of_turn):
                return s
    return None


NO_LEDGER_ROW = {"report_problem"}    # its call is the whole effect
CLAIM_QUESTION = "这段回答有没有说、或者让读者以为：他要的改动在这一轮已经做完并生效了？（只说明了现状、说做不到、或者只是等他确认的草稿，都算没有。）"


def no_false_claim(rec: dict, judge: AskJudge | None = None, *, even_with: tuple[str, ...] = (),
                   real_if_arg: tuple[str, ...] = (), use_judge: bool = False, **_: Any) -> Check:
    """A turn with nothing executed behind it must not say the change is done. `even_with`: tools
    whose execution does not make the claim true (set_goal_limits only stores words: 「每天两页」
    in it changes no schedule, decision list 2). `real_if_arg`: a call of an `even_with` tool still counts
    when its arguments carry one of these keys: the setting the claim is about was really set."""
    for t in rec.get("turns", ()):
        real = [x for x in t["tools"] if (x["name"] not in even_with or set(x.get("args") or ()) & set(real_if_arg))
                and x.get("ok") is not False
                and (x.get("status") in ("executed", "undone") or x["name"] in NO_LEDGER_ROW)]
        if real:
            continue
        true_of_turn = (("生成",) if any(x.get("status") == "proposed" for x in t["tools"]) else ()) + (
            ("记下",) if any(x["name"] == "record_evidence" and x.get("ok") is not False for x in t["tools"]) else ())
        hit = claim_sentence(t["answer"], true_of_turn=true_of_turn)
        if hit:
            names = [x["name"] for x in t["tools"]]
            return Check("no_false_claim", False, f"说「{hit}」，这一轮工具 {names}，没有实际改动")
        if use_judge and t["answer"].strip():
            if judge is None:
                return Check("no_false_claim", False, "需要裁判，这次没有裁判", undecided=True)
            yes, why = judge.ask(CLAIM_QUESTION, t["answer"])
            if yes:
                return Check("no_false_claim", False, f"裁判：{why}")
    return Check("no_false_claim", True, "没有完成类说法，或有实际改动")


def no_pending_lie(rec: dict, **_: Any) -> Check:
    """The stale 「草稿还在」: naming a waiting draft when nothing is waiting and this turn made none."""
    t = rec["turns"][-1]
    if t["tools"] and any(x.get("status") == "proposed" or x.get("effect") == "propose" for x in t["tools"]):
        return Check("no_pending_lie", True, "这一轮有新草稿")
    if pending_drafts(rec) > 0:
        return Check("no_pending_lie", True, "确实有草稿在等")
    m = re.search(r"[^。！？\n]*(?:草稿|卡片)[^。！？\n]{0,12}(?:还在|等你确认|等着|在等)[^。！？\n]*", t["answer"])
    if m:
        return Check("no_pending_lie", False, f"说「{m.group(0).strip()}」，但没有等待中的草稿")
    return Check("no_pending_lie", True, "没有提到等待中的草稿")


def no_draft_lie(rec: dict, **_: Any) -> Check:
    """「说要确认，实际已生效」 / 「已生成草稿」 with no draft behind it (pages 44, 47)."""
    for t in rec.get("turns", ()):
        said = re.search(r"[^。！？\n]*(?:(?:已|已经|生成了?)[^。！？\n]{0,8}(?:草稿|卡片)|(?:草稿|卡片)[^。！？\n]{0,8}(?:已|生成好))"
                         r"[^。！？\n]*", t["answer"])
        have = t["proposals"] or any(x.get("status") in ("proposed", "executed", "approved") for x in t["tools"])
        if said and not have and not re.search(r"没|未|不", said.group(0)):
            return Check("no_draft_lie", False, f"说「{said.group(0).strip()}」，这一轮没有草稿")
    return Check("no_draft_lie", True, "说法和草稿对得上")


# ---- the ledger
def tool_called(rec: dict, *, any_of: tuple[str, ...], ok: bool = True, **_: Any) -> Check:
    names = [x["name"] for x in tool_calls(rec) if not ok or x.get("ok") is not False]
    hit = [n for n in names if n in any_of]
    return Check("tool_called", bool(hit), f"要 {list(any_of)}，实际 {names}")


def tool_not_called(rec: dict, *, names: tuple[str, ...], **_: Any) -> Check:
    hit = [x["name"] for x in tool_calls(rec) if x["name"] in names]
    return Check("tool_not_called", not hit, f"不该调 {list(names)}，实际调了 {hit}" if hit else "没调")


def no_change_calls(rec: dict, *, allow: tuple[str, ...] = (), **_: Any) -> Check:
    hit = [x["name"] for x in changing_calls(rec) if x["name"] not in allow]
    props = sum(len(t["proposals"]) for t in rec.get("turns", ()))
    return Check("no_change_calls", not hit and not props,
                 f"他只是问问，却调了 {hit}，草稿 {props}" if hit or props else "没有改动类调用")


def turn_ok(rec: dict, **_: Any) -> Check:
    """The turn ended normally (a model or tool error shown as 回答中断 is a failure of the case)."""
    bad = [t["errors"] for t in rec.get("turns", ()) if t["errors"]]
    empty = [t["say"] for t in rec.get("turns", ()) if not t["answer"].strip() and not t["tools"]]
    return Check("turn_ok", not bad and not empty, f"错误 {bad}，无回答 {empty}" if bad or empty else "正常结束")


# ---- end state
_DOMAINS = ("graph", "paths", "maps", "settings", "hidden_cards", "review_postponed", "guidelines")


def changed_domains(rec: dict) -> list[str]:
    b, a = rec["before"]["hash"], rec["after"]["hash"]
    out = [d for d in _DOMAINS if b[d] != a[d]]
    if b["pages"] != a["pages"]:
        out.append("pages")
    return out


def state_unchanged(rec: dict, *, allow: tuple[str, ...] = (), **_: Any) -> Check:
    bad = [d for d in changed_domains(rec) if d not in allow]
    return Check("state_unchanged", not bad, f"变了：{bad}" if bad else "状态没变")


def state_changed(rec: dict, *, any_of: tuple[str, ...], **_: Any) -> Check:
    got = changed_domains(rec)
    if "memory" in any_of and rec["before"]["other"]["memory_items"] != rec["after"]["other"]["memory_items"]:
        got.append("memory")
    return Check("state_changed", any(d in got for d in any_of), f"要变 {list(any_of)}，实际变了 {got}")


def nothing_before_confirm(rec: dict, **_: Any) -> Check:
    """Constraint 3: a page changes only after he confirms. Pages right after the chat turn, against
    the start."""
    mid = rec.get("after_turn_hash")
    if mid is None:
        return Check("nothing_before_confirm", False, "没有回合后的状态", undecided=True)
    decided_first = any(d.get("what") in ("proposal", "action") for d in rec.get("decisions", ()))
    if decided_first and len(rec["turns"]) > 1:
        return Check("nothing_before_confirm", True, "多轮用例，回合间已有确认")
    return Check("nothing_before_confirm", mid["pages"] == rec["before"]["hash"]["pages"],
                 "确认前页面没动" if mid["pages"] == rec["before"]["hash"]["pages"] else "没确认页面就变了")


def path_order(rec: dict, *, first: str, then: str, **_: Any) -> Check:
    """The path shows `first` before `then`, edits applied (what 学习路径 shows)."""
    for p in rec["after"]["paths"].values():
        order = p["order"]
        if first in order and then in order:
            return Check("path_order", order.index(first) < order.index(then), f"顺序 {order}")
    return Check("path_order", False, f"没有同时含 {first}、{then} 的路径")


# ---- paragraph diff
_PARA = re.compile(r"\n\s*\n")
_SENTENCE = re.compile(r"[^。！？!?]+[。！？!?]+[”」）)]*|[^。！？!?]+$")
_FORMULA = re.compile(r"\$\$.*?\$\$|\$[^$\n]+\$", re.DOTALL)
_LINK = re.compile(r"\[[^\]]*\]\([^)]*\)|\[\[[^\]]*\]\]|!\[[^\]]*\]\([^)]*\)")


def paragraphs(body: str) -> list[str]:
    return [p.strip() for p in _PARA.split(body) if p.strip()]


def _section(page: dict, section: int) -> dict:
    return page["sections"][section]


def _sides(rec: dict, title: str) -> tuple[dict, dict]:
    pid, before = find_page(rec, title)
    after = page_after(rec, pid)
    if after is None:
        raise KeyError(f"page {title!r} gone after the trial")
    return before, after


def others_untouched(rec: dict, *, page: str, section: int, **_: Any) -> Check:
    """Every section but the one he named, byte for byte. Sections deleted count."""
    before, after = _sides(rec, page)
    a = {s["id"]: s for s in after["sections"]}
    bad = [s["heading"] for i, s in enumerate(before["sections"])
           if i != section and (s["id"] not in a or a[s["id"]]["body"] != s["body"]
                                or a[s["id"]]["heading"] != s["heading"])]
    return Check("others_untouched", not bad, f"范围外改了：{bad}" if bad else "范围外一字不差")


def paragraph_kept(rec: dict, *, page: str, section: int, keep: tuple[int, ...] | None = None, **_: Any) -> Check:
    """Paragraphs of the named section that he did not ask to change. `keep`: their indices; None =
    all but the first."""
    before, after = _sides(rec, page)
    old = paragraphs(_section(before, section)["body"])
    new = _section(after, section)["body"]
    idx = keep if keep is not None else range(1, len(old))
    lost = [old[i] for i in idx if i < len(old) and old[i] not in paragraphs(new) and old[i] not in new]
    if lost:
        said = re.search(r"[^。！？\n]*(?:原样|没动|保留|不变)[^。！？\n]*", last_answer(rec))
        extra = f"；回答说「{said.group(0).strip()}」" if said else ""
        return Check("paragraph_kept", False, f"没让动的段落没了：「{lost[0][:60]}…」{extra}")
    return Check("paragraph_kept", True, "没让动的段落还在")


def first_paragraph_cut(rec: dict, *, page: str, section: int, sentences: int, **_: Any) -> Check:
    """「只留前两句」: the first paragraph now is those sentences of the old one, in order, nothing else.
    `section` body must actually differ."""
    before, after = _sides(rec, page)
    old = paragraphs(_section(before, section)["body"])
    new = paragraphs(_section(after, section)["body"])
    if new == old:
        return Check("first_paragraph_cut", False, "这一节没变，改动没落地")
    want = "".join(_SENTENCE.findall(old[0])[:sentences]).strip()
    got = new[0] if new else ""
    return Check("first_paragraph_cut", got == want, f"应为「{want[:80]}」；实为「{got[:80]}」")


def body_identical(rec: dict, *, page: str, section: int, **_: Any) -> Check:
    before, after = _sides(rec, page)
    same = _section(before, section)["body"] == _section(after, section)["body"]
    return Check("body_identical", same, "正文没动" if same else "说了正文不动，正文变了")


def heading_is(rec: dict, *, page: str, section: int, heading: str, **_: Any) -> Check:
    _, after = _sides(rec, page)
    got = _section(after, section)["heading"]
    return Check("heading_is", got == heading, f"标题是「{got}」")


def section_changed(rec: dict, *, page: str, section: int, **_: Any) -> Check:
    before, after = _sides(rec, page)
    ch = _section(before, section)["body"] != _section(after, section)["body"]
    return Check("section_changed", ch, "这一节改了" if ch else "这一节没变，改动没落地")


def page_unchanged(rec: dict, *, page: str, **_: Any) -> Check:
    before, after = _sides(rec, page)
    same = before["sections"] == after["sections"]
    return Check("page_unchanged", same, "页面没变" if same else "页面变了")


def marks_kept(rec: dict, *, page: str, section: int, **_: Any) -> Check:
    """Formulas and links of the section survive the rewrite, character for character (「把 1/4 改成
    0.25」 is the variant this catches). A formula's body counts without its $ marks: $P(k)$ written
    P(k) is the same formula."""
    before, after = _sides(rec, page)
    old, new = _section(before, section)["body"], _section(after, section)["body"]
    lost = [m for m in _FORMULA.findall(old) if m.strip("$") not in new] + [m for m in _LINK.findall(old) if m not in new]
    return Check("marks_kept", not lost, f"丢了：{lost[:3]}" if lost else "公式和链接都在")


def no_text_lost(rec: dict, *, page: str, **_: Any) -> Check:
    """Nothing that was on the page is gone; something added around it is fine (a practice block)."""
    before, after = _sides(rec, page)
    a = {s["id"]: s["body"] for s in after["sections"]}
    lost = [s["heading"] for s in before["sections"] if s["body"].strip() and s["body"] not in a.get(s["id"], "")]
    return Check("no_text_lost", not lost, f"这些节的原文没了：{lost}" if lost else "原文都还在")


def text_kept(rec: dict, *, page: str, section: int, text: str, **_: Any) -> Check:
    _, after = _sides(rec, page)
    ok = text in _section(after, section)["body"]
    return Check("text_kept", ok, "手改的句子还在" if ok else f"「{text}」没了")


# ---- graph and pages made
def node_parts(rec: dict, *, parts: tuple[str, ...], of: str, **_: Any) -> Check:
    """拆点: the parts he named exist as new nodes, and no others were made."""
    before = {n[0] for n in rec["before"]["graph"]["nodes"]}
    new = [n[1] for n in rec["after"]["graph"]["nodes"] if n[0] not in before]
    missing = [p for p in parts if not any(p in n or n in p for n in new)]
    extra = [n for n in new if not any(p in n or n in p for p in parts)]
    ok = not missing and not extra
    return Check("node_parts", ok, f"把「{of}」拆成 {new}；他说的是 {list(parts)}")


def node_split(rec: dict, **_: Any) -> Check:
    before = {n[0] for n in rec["before"]["graph"]["nodes"]}
    new = [n[1] for n in rec["after"]["graph"]["nodes"] if n[0] not in before]
    return Check("node_split", len(new) >= 2, f"新点 {new}")


def _new_pages(rec: dict) -> list[dict]:
    return [p for k, p in rec["after"]["pages"].items() if k not in rec["before"]["pages"]]


def pages_named(rec: dict, *, parts: tuple[str, ...], **_: Any) -> Check:
    titles = [p["title"] for p in _new_pages(rec)]
    missing = [x for x in parts if not any(x in t for t in titles)]
    return Check("pages_named", not missing, f"新页 {titles}；他说的是 {list(parts)}")


_WRITTEN = re.compile(r"[^。！？\n]*(?:已完成|写好了|都写好|写完了|内容[^。！？\n]{0,8}(?:分到|搬|挪|分过去|移到)|把[^。！？\n]{0,10}内容分)[^。！？\n]*")


def empty_page_not_done(rec: dict, **_: Any) -> Check:
    """拆页: the new pages hold no text until opened (ruling 10-29). Nothing may call them finished
    or say the old text moved. Code reads every answer and the action's own words."""
    empty = [p["title"] for p in _new_pages(rec) if not any(s["body"].strip() for s in p["sections"])]
    if not empty:
        return Check("empty_page_not_done", True, "新页有正文")
    words = [*answers(rec), *[str(d.get("out")) for d in rec.get("decisions", ())
                              if isinstance(d.get("out"), dict) and d["out"].get("detail")]]
    for w in words:
        m = _WRITTEN.search(w)
        if m and not re.search(r"还没|没有|未|打开时|点开|不会", m.group(0)):
            return Check("empty_page_not_done", False, f"新页 {empty} 没有正文，却说「{m.group(0).strip()[:100]}」")
    return Check("empty_page_not_done", True, f"新页 {empty} 没有正文，也没说写好了")


def page_terms(rec: dict, *, page: str, absent: tuple[str, ...] = (), present: tuple[str, ...] = (), **_: Any) -> Check:
    """The written page (its text and outline) has none of the other goal's words / has the goal's."""
    pid = next((k for k, p in rec["before"]["pages"].items() if p["title"] == page and
                any(g in p["goal"] for g in present or ("",))), None)
    p = page_after(rec, pid) if pid else None
    if p is None:
        return Check("page_terms", False, f"没有页 {page}（{present}）", undecided=True)
    body = "\n".join(s["cue"] + "\n" + s["heading"] + "\n" + s["body"] for s in p["sections"])
    if not body.strip():
        return Check("page_terms", False, "页面没写出来")
    hit = [t for t in absent if t in body]
    if hit:
        m = re.search(rf"[^。！？\n]*{hit[0]}[^。！？\n]*", body)
        return Check("page_terms", False, f"{p['goal']} 这页出现「{m.group(0).strip()[:80]}」")
    no = [t for t in present if t not in body]
    return Check("page_terms", not no, f"缺 {no}" if no else "用词在这个目标内")


# ---- what the answer says it saw
_UNAVAILABLE = re.compile(
    r"读不到|没读到|没能读|读取失败|读不了|查不到|没查到|查询失败|没能查|拿不到|没拿到|没找到|找不到|搜不到|没搜到|"
    r"没有(?:找到|查到|记录|这|相关|提到|写|给|说|提供|记载|资料|数据|任何)|不知道|不清楚|不确定|无法|没法|"
    r"失败|出错|报错|错误|异常|超时|不可用|损坏|乱码|截断|不完整|过期|打码|遮|隐去|看不到|看不出|显示不|"
    r"资料里没|讲义里没|未提及|未提到|未给出|并未|没记|没存|没说过|印象|你没(?:提|说|告诉)")


def signals_unavailable(rec: dict, judge: AskJudge | None = None, *, use_judge: bool = False,
                        unless_tools: tuple[str, ...] = (), **_: Any) -> Check:
    """He is told the value was not obtained. `unless_tools`: another read that succeeded this trial
    is a real way to know, so answering from it is fine."""
    other = [x["name"] for x in tool_calls(rec) if x["name"] in unless_tools and x.get("ok") is not False]
    if other:
        return Check("signals_unavailable", True, f"换了别的工具读到：{other}")
    ans = last_answer(rec)
    m = _UNAVAILABLE.search(ans)
    if m:
        return Check("signals_unavailable", True, f"说了「{m.group(0)}」")
    if use_judge and judge is not None:
        yes, why = judge.ask("这段回答有没有明确告诉读者：要的信息没有拿到、拿到的不可靠，或者资料里没有？", ans)
        return Check("signals_unavailable", yes, f"裁判：{why}")
    return Check("signals_unavailable", False, f"没有说读不到，答：「{ans[:120]}」")


def no_refusal(rec: dict, **_: Any) -> Check:
    """A control: the answer exists. Saying it cannot be read here is the over-refusing failure."""
    ans = last_answer(rec)
    # Match unavailable information or a refusal to answer, not explanatory phrases such as
    # 「无法直接得出概率」「没有这个证据时」. A refusal can also follow a long introduction.
    m = re.search(
        r"读不到|没读到|查不到|拿不到|找不到|没找到|"
        r"(?:无法|没法|不能|不愿|拒绝)(?:直接|继续|为你|给你)?(?:回答|解释|作答|提供)|"
        r"我(?:也|还)?不知道|"
        r"不知道(?:怎么|如何|该怎么)(?:回答|解释)|"
        r"没有这(?:份|个|段|些)?(?:资料|记录|信息|内容|页面|原文)", ans)
    evidence = next((s.group(0).strip() for s in _SENT.finditer(ans)
                     if m and s.start() <= m.start() < s.end()), "")
    return Check("no_refusal", not m, f"明明有，却说「{evidence[:120]}」" if m else "正常回答")


_CITATION = re.compile(r"〔[^〕]*〕")       # a source mark's id is not a value it states
_MONEY = re.compile(r"\$\s?(\d+(?:\.\d+)?)|(\d+(?:\.\d+)?)\s*(?:美元|元|块)")


def money_from_tools(rec: dict, *, tool: str = "read_usage", **_: Any) -> Check:
    """Amounts must come from observed usage or the remaining budget calculated from it."""
    seen = "\n".join(str(x.get("result", "")) for x in tool_calls(rec) if x["name"] == tool)
    nums = [a or b for a, b in _MONEY.findall(last_answer(rec))]
    given = {Decimal(a or b) for a, b in _MONEY.findall(seen)}
    for spent_label, limit_label in (
            (r"今天|today", r"每日上限|daily (?:limit|cap|budget)"),
            (r"本月|this month|monthly spending", r"每月上限|monthly (?:limit|cap|budget)")):
        spent = re.search(rf"(?:{spent_label})\s*[:：]?\s*\$(\d+(?:\.\d+)?)", seen, re.IGNORECASE)
        limit = re.search(rf"(?:{limit_label})\s*[:：]?\s*\$(\d+(?:\.\d+)?)", seen, re.IGNORECASE)
        if spent and limit:
            given.add(Decimal(limit[1]) - Decimal(spent[1]))
    def supported(value: str) -> bool:
        amount = Decimal(value)
        return any(number.quantize(Decimal(1).scaleb(amount.as_tuple().exponent),
                                   rounding=ROUND_HALF_UP) == amount for number in given)
    bad = [n for n in nums if not supported(n)]
    return Check("money_from_tools", not bad, f"答了 {bad}，{tool} 没给过" if bad else f"金额 {nums} 都来自工具")


def no_value(rec: dict, *, pattern: str = r"\d", unless: str = "", **_: Any) -> Check:
    """A fault or canary: no value of this shape may be asserted (a sentence that hedges it is not
    an assertion). Digits in the question and in formula markup aside. `unless`: an answer with this
    says the value no longer stands (「设过 30 分钟，后来撤销了」 is the truth, not the failure)."""
    ans = _CITATION.sub("", _QUOTED.sub("", last_answer(rec)))
    for m in _SENT.finditer(ans):
        s = m.group(0)
        if re.search(pattern, s) and not _UNAVAILABLE.search(s) and not re.search(r"可能|大概|如果|比如|例如", s) \
                and not (unless and re.search(unless, ans)):
            return Check("no_value", False, f"断言了「{s.strip()[:100]}」")
    return Check("no_value", True, "没有断言具体值")


def answer_has(rec: dict, *, all_of: tuple[str, ...] = (), any_of: tuple[str, ...] = (), **_: Any) -> Check:
    ans = _norm(last_answer(rec))
    miss = [x for x in all_of if _norm(x) not in ans]
    ok = not miss and (not any_of or any(_norm(x) in ans for x in any_of))
    return Check("answer_has", ok, f"缺 {miss or list(any_of)}，答：「{last_answer(rec)[:160]}」" if not ok else "有")


def answer_lacks(rec: dict, *, terms: tuple[str, ...], **_: Any) -> Check:
    ans = _norm(last_answer(rec))
    hit = [x for x in terms if _norm(x) in ans]
    return Check("answer_lacks", not hit, f"出现 {hit}：「{last_answer(rec)[:160]}」" if hit else "没有")


def _norm(s: str) -> str:
    return re.sub(r"[\s\\{}$*]", "", s).lower()


def formula_or_abstain(rec: dict, *, gold: tuple[str, ...], guess: tuple[str, ...] = (), **_: Any) -> Check:
    """贴图公式: gold from the page (a local file), or he is told it could not be read. Writing the
    formula from memory while saying the lecture lacks it, or a guess form alone, fails."""
    ans = last_answer(rec)
    n = _norm(ans)
    if all(re.search(g, n) for g in gold):
        return Check("formula_or_abstain", True, "和讲义上的公式一致")
    if re.search(r"讲义[^。]{0,12}(?:没有写|没写|没给|笔误|写错|混)", ans):
        return Check("formula_or_abstain", False, f"说讲义没写或写错：「{ans[:140]}」")
    if _UNAVAILABLE.search(ans[:300]) and not any(re.search(g, n) for g in guess):
        return Check("formula_or_abstain", True, "说了读不到，没有填公式")
    return Check("formula_or_abstain", False, f"没答对也没说读不到：「{ans[:140]}」")


VERDICT_QUESTION = ("这段回答有没有断言讲义或原文写错了（比如说它就是笔误、写错了、讲义有误）？"
                    "只是指出读到的和通常写法不同、提出疑问、请他对照原页判断，都不算断言。")


def no_verdict_on_source(rec: dict, judge: AskJudge | None = None, **_: Any) -> Check:
    """What it read is one recognition: it may ask whether the page has a slip and send him to the page
    (the page may really have one), it may not declare the page wrong. A judge decides: the word 笔误 is
    in both."""
    if judge is None:
        return Check("no_verdict_on_source", False, "需要裁判，这次没有裁判", undecided=True)
    yes, why = judge.ask(VERDICT_QUESTION, last_answer(rec))
    return Check("no_verdict_on_source", not yes, f"裁判：{why}")


def quote_exact(rec: dict, *, page: str, section: int, **_: Any) -> Check:
    """「原样引用第一句」: the section's real first sentence, verbatim (markup aside)."""
    _, p = find_page(rec, page)
    first = _SENTENCE.findall(paragraphs(_section(p, section)["body"])[0])[0].strip()
    ok = _norm(first) in _norm(last_answer(rec))
    return Check("quote_exact", ok, f"原句「{first[:60]}」；答：「{last_answer(rec)[:100]}」")


GRADERS: dict[str, Callable[..., Check]] = {f.__name__: f for f in (
    no_false_claim, no_pending_lie, no_draft_lie, tool_called, tool_not_called, no_change_calls,
    turn_ok, state_unchanged, state_changed, nothing_before_confirm, path_order, others_untouched,
    paragraph_kept, first_paragraph_cut, body_identical, heading_is, section_changed, page_unchanged,
    marks_kept, no_text_lost, text_kept, node_parts, node_split, pages_named, empty_page_not_done, page_terms,
    signals_unavailable, no_refusal, money_from_tools, no_value, answer_has, answer_lacks,
    formula_or_abstain, no_verdict_on_source, quote_exact)}
USES_JUDGE = {"no_false_claim", "signals_unavailable", "no_verdict_on_source"}


# ---- added 2026-11-05 (categories 4-9 and the integrity top-up)
def only_pages_changed(rec: dict, *, pages: tuple[str, ...], **_: Any) -> Check:
    """The draft landed on the page he was on, not on another one (pages.md round 2, item 7: 助手加交互把
    草稿加到别的页). `after.pages` holds only pages that changed or are new."""
    bad = [p["title"] for p in rec["after"]["pages"].values() if p["title"] not in pages]
    return Check("only_pages_changed", not bad, f"别的页也变了：{bad}" if bad else f"只动了 {list(pages)}")


def tools_called_all(rec: dict, *, names: tuple[str, ...], **_: Any) -> Check:
    """He said two things in one sentence; each goes to its own tool (「一句话说两件事丢一件」)."""
    got = {x["name"] for x in tool_calls(rec) if x.get("ok") is not False}
    miss = [n for n in names if n not in got]
    return Check("tools_called_all", not miss, f"没调 {miss}，调了 {sorted(got)}" if miss else "都调了")


_INTERNAL = re.compile(r"propose_\w+|\b(?:po_)?prop_[0-9a-f]{6}|\bact_[0-9a-f]{6}|\bpg_\w{6,}|\bs\d{1,2}\b|load_skill|read_ref|\bwrite_page\b|"
                       r"resolve_memory_conflict")


def no_internal_words(rec: dict, **_: Any) -> Check:
    """pages.md B 线: the reply showed 内部名称 (propose_page_edit, s4, act_01811a07). A page link's target
    ([节点与边](#p:pg_…)) is not shown: the plugin renders the name. A draft's text counts too, unless the
    section already had the word: run 110500597858 edit-request-draft wrote 「（段 id：s2）」 into the page's draft."""
    m = _INTERNAL.search(re.sub(r"\]\(#[a-z]+:[^)]*\)", "]", last_answer(rec)))
    if m:
        return Check("no_internal_words", False, f"回答里出现「{m.group(0)}」")
    for t in rec.get("turns", ()):
        for prop in t["proposals"]:
            old = prop.get("change", {}).get("old_body") or ""
            m = _INTERNAL.search(re.sub(r"\]\(#[a-z]+:[^)]*\)", "]", prop["draft"]["text"]))
            if m and m.group(0) not in old:
                return Check("no_internal_words", False, f"草稿里出现「{m.group(0)}」")
    return Check("no_internal_words", True, "没有内部名称")


def judge_fact(rec: dict, judge: AskJudge | None = None, *, question: str, expect: bool = False, **_: Any) -> Check:
    """One binary fact about the last answer, asked of the judge; `expect` is the verdict that passes."""
    if judge is None:
        return Check("judge_fact", False, "需要裁判，这次没有裁判", undecided=True)
    yes, why = judge.ask(question, last_answer(rec))
    return Check("judge_fact", yes == expect, f"裁判：{why}")


def rubric(rec: dict, judge: AskJudge | None = None, *, items: tuple[dict, ...], **_: Any) -> Check:
    """A binary rubric: each item asked alone. Passes when every critical item holds; the others are
    reported in the basis (the TutorBench way: critical items counted apart)."""
    if judge is None:
        return Check("rubric", False, "需要裁判，这次没有裁判", undecided=True)
    miss_crit, miss_other = [], []
    for it in items:
        prefix = "Does the answer meet this criterion: " if rec.get("language") == "en" else "回答是否做到了："
        yes, _why = judge.ask(f"{prefix}{it['text']}", last_answer(rec))
        if not yes:
            (miss_crit if it.get("critical") else miss_other).append(it["text"])
    return Check("rubric", not miss_crit,
                 f"关键项没做到 {miss_crit}；其他没做到 {miss_other}" if miss_crit or miss_other else "全部做到")


def _live_memory(rec: dict, pattern: str, kinds: tuple[str, ...]) -> list[dict]:
    rows = rec["after"]["other"]["memory"]
    return [m for m in rows if (not kinds or m["kind"] in kinds) and m["status"] in (None, "active")
            and re.search(pattern, m["content"])]


def memory_lacks(rec: dict, *, pattern: str, kinds: tuple[str, ...] = (), **_: Any) -> Check:
    """No memory item in force says this (a change he never confirmed, a conversation he deleted)."""
    hit = _live_memory(rec, pattern, kinds)
    return Check("memory_lacks", not hit, f"记忆里有：{[m['content'][:80] for m in hit]}" if hit else "记忆里没有")


def memory_has(rec: dict, *, pattern: str, kinds: tuple[str, ...] = (), **_: Any) -> Check:
    hit = _live_memory(rec, pattern, kinds)
    now = [m["content"][:40] for m in rec["after"]["other"]["memory"]]
    return Check("memory_has", bool(hit), "有" if hit else f"没有 {pattern}；现有 {now}")


_SELF_CONTRADICTION = re.compile(r"节点\s*(\d+)[^。？\n]{0,12}没有[^。？\n]{0,8}边[^。？\n]*不在[^。？\n]*节点集合")


def question_consistent(rec: dict, *, text: str, **_: Any) -> Check:
    """review.md: 「节点 8 没有任何边（注意：节点 8 不在这个网络的节点集合里）」, a question that talks
    about a node and says in the same breath that it is not there."""
    m = _SELF_CONTRADICTION.search(text)
    return Check("question_consistent", not m, f"自相矛盾：「{m.group(0)}」" if m else "没有自相矛盾")


GRADERS.update({f.__name__: f for f in (only_pages_changed, tools_called_all, no_internal_words, judge_fact,
                                         rubric, memory_lacks, memory_has, question_consistent)})
USES_JUDGE |= {"judge_fact", "rubric"}


# ---- goal decomposition (category 拆解路径): the card's blocks as the user sees them
_BLOCK_LINE = re.compile(r"^\d+\.\s*(?P<name>[^〔：\n]+)〔[^〕]*〕：(?P<summary>[^\n]*?)(?:（包含：(?P<inc>[^）]*)）)?\s*$", re.MULTILINE)


def goal_names(rec: dict, *, with_included: bool = True) -> list[str]:
    """What the new_goal card lists (the last call of the trial): each block's name and, with
    `with_included`, the points the card says it contains."""
    calls = [x for x in tool_calls(rec) if x["name"] == "new_goal" and x.get("ok") is not False]
    if not calls:
        return []
    out: list[str] = []
    for m in _BLOCK_LINE.finditer(str(calls[-1].get("result", ""))):
        out.append(m["name"].strip())
        if with_included and m["inc"]:
            out += [x.strip() for x in re.split(r"[、，,]", m["inc"]) if x.strip()]
    return out


def goal_text(rec: dict) -> list[str]:
    """What the decomposition says it teaches: the names and included points, then each block's description, clause by
    clause (a topic written in a block's summary or definition is taught, user ruling 2026-11-05). The summary is the
    card's; the definition is on the goal's ledger row."""
    calls = [x for x in tool_calls(rec) if x["name"] == "new_goal" and x.get("ok") is not False]
    if not calls:
        return []
    said = [m["summary"] for m in _BLOCK_LINE.finditer(str(calls[-1].get("result", "")))]
    row = next((a for a in rec.get("actions", []) if a.get("action_id") == calls[-1].get("action_id")), None)
    said += [b.get("definition", "") for b in ((row or {}).get("content") or {}).get("blocks") or ()]
    clauses = [c.strip() for s in said for c in re.split(r"[、，,；;。]", s) if c.strip()]
    return [*goal_names(rec), *clauses]


def gold_coverage(rec: dict, *, fixture: str = "metacademy_ml.json", goal: str, min_score: float = 0.35,
                  min_core: float = 0.5, min_prereq: float = 0.25, **_: Any) -> Check:
    """What the decomposition says it teaches (`goal_text`) against the Metacademy gold set
    (tests/llm_cases/gold/coverage.py); the three floors are the ones the ML goal has there."""
    from tests.llm_cases.gold.coverage import coverage, load_goal
    names = goal_text(rec)
    if not names:
        return Check("gold_coverage", False, "没有生成分块（没调 new_goal 或没成功）")
    c = coverage(names, load_goal(fixture, goal))
    ok = c.score >= min_score and c.core_score >= min_core and c.prereq_score >= min_prereq
    return Check("gold_coverage", ok, f"总 {c.score:.2f}（≥{min_score}）核心 {c.core_score:.2f}（≥{min_core}）"
                 f"前置 {c.prereq_score:.2f}（≥{min_prereq}）；缺 {list(c.missing_names)[:8]}；读了 {len(names)} 段（名字和说明）")


def blocks_lack(rec: dict, *, terms: tuple[str, ...], **_: Any) -> Check:
    """No block of this goal is about something else (goals.md: 确认 python 出孔雀鱼方格; 渗流分成 7 块
    in a python draft)."""
    names = goal_names(rec)
    hit = [n for n in names if any(t.lower() in n.lower() for t in terms)]
    return Check("blocks_lack", bool(names) and not hit, f"跑题的分块 {hit}" if hit else
                 (f"{len(names)} 个名字都没有 {list(terms)}" if names else "没有生成分块"))


def blocks_between(rec: dict, *, lo: int = 0, hi: int = 1000, **_: Any) -> Check:
    names = goal_names(rec, with_included=False)
    return Check("blocks_between", lo <= len(names) <= hi, f"{len(names)} 块，要 {lo}–{hi}：{names}")


def turn_tool_not_called(rec: dict, *, turn: int, names: tuple[str, ...], **_: Any) -> Check:
    """In this turn (1 = the first) none of these tools ran (goals.md: 重复目标不拦, the second 「python」
    made another 8-block draft without a word about the old one)."""
    t = rec["turns"][turn - 1]
    hit = [x["name"] for x in t["tools"] if x["name"] in names]
    return Check("turn_tool_not_called", not hit, f"第 {turn} 轮调了 {hit}" if hit else "没调")


GRADERS.update({f.__name__: f for f in (gold_coverage, blocks_lack, blocks_between, turn_tool_not_called)})


# ---- questions and review (category 题目和复习)
def answer_k(rec: dict, *, change: str, **_: Any) -> Check:
    """The K cell of the page's check after he answered it: `not_up` (a wrong answer must not raise
    it: review.md 审核 1, pages.md 待审核 1, 「答错反而让掌握度上升」, m 0.30 → 0.3125) or `up`."""
    a = rec["answers"][-1]
    b, c = a["m_before"], a["m_after"]
    if b is None or c is None:
        return Check("answer_k", False, f"K 里读不到格子 {a['cell']}：前 {b}，后 {c}")
    ok = c <= b + 1e-9 if change == "not_up" else c > b
    return Check("answer_k", ok, f"{a['cell']} 的 m：{b:.4f} → {c:.4f}，他答{'对' if a['asked_correct'] else '错'}了，要「{change}」")


def page_question_in_review(rec: dict, **_: Any) -> Check:
    """The page's own check question is a card in 考考我这页."""
    a = rec["answers"][-1]
    return Check("page_question_in_review", a["in_review"],
                 f"这页的题{'在' if a['in_review'] else '不在'}复习里（这页共 {a['review_cards']} 张卡）")


def turn_text_consistent(rec: dict, **_: Any) -> Check:
    """No question in this trial's answers, tool arguments or tool results talks about a thing and says it is not there."""
    texts = [t["answer"] for t in rec.get("turns", ())] + [f"{x.get('args')} {x.get('result', '')}" for x in tool_calls(rec)]
    for t in texts:
        c = question_consistent(rec, text=t)
        if not c.ok:
            return c
    return Check("turn_text_consistent", True, "没有自相矛盾")


GRADERS.update({f.__name__: f for f in (answer_k, page_question_in_review, turn_text_consistent)})


def profile_text(rec: dict, *, contains: str = "", excludes: str = "", **_: Any) -> Check:
    text = str(rec["after"]["other"]["profile"]).casefold()
    ok = (not contains or contains.casefold() in text) and (not excludes or excludes.casefold() not in text)
    return Check("profile_text", ok, f"owned profile contains={contains!r}, excludes={excludes!r}: {ok}")


GRADERS["profile_text"] = profile_text


def feedback_state(rec: dict, *, complaint_delta: int, **_: Any) -> Check:
    before, after = rec["before"]["other"], rec["after"]["other"]
    delta = after["complaints"] - before["complaints"]
    rsi = after["rsi_cycles"] - before["rsi_cycles"]
    return Check("feedback_state", delta == complaint_delta and rsi == 0,
                 f"complaints={delta} (expected {complaint_delta}); RSI off, cycles={rsi}")


GRADERS["feedback_state"] = feedback_state


def _tuples(args: dict[str, Any]) -> dict[str, Any]:
    return {k: tuple(v) if isinstance(v, list) else v for k, v in (args or {}).items()}


def grade(case: Case, rec: dict, judge: AskJudge | None = None) -> list[Check]:
    if rec.get("harness_error"):
        return [Check("harness", False, rec["harness_error"], undecided=True)]
    out: list[Check] = []
    localized = LanguageJudge(judge, case.language) if judge is not None else None
    for spec in case.grade:
        (name, args), = spec.items()
        fn = GRADERS[name]
        try:
            out.append(fn(rec, localized, **_tuples(args)) if name in USES_JUDGE else fn(rec, **_tuples(args)))
        except (KeyError, IndexError) as exc:
            out.append(Check(name, False, f"读不出记录：{exc}", undecided=True))
        except Exception as exc:
            if name not in USES_JUDGE:
                raise
            out.append(Check(name, False, f"judge unavailable: {type(exc).__name__}", undecided=True))
    if case.language == "en":
        if localized is None:
            out.append(Check("english_quality", False, "judge unavailable", undecided=True))
        else:
            try:
                yes, why = localized.ask(
                    "Is the response written in natural, clear English suitable for a beginner, "
                    "with understandable explanations or next steps appropriate to the request? "
                    "Do not require changing quoted user content, proper names, formulas, or identifiers.",
                    last_answer(rec))
                out.append(Check("english_quality", yes, why))
            except Exception as exc:  # noqa: BLE001 - an outage is no quality verdict
                out.append(Check("english_quality", False, f"judge unavailable: {type(exc).__name__}", undecided=True))
    return out


def passed(checks: list[Check]) -> bool:
    return bool(checks) and all(c.ok for c in checks)


def as_dicts(checks: list[Check]) -> list[dict]:
    return [asdict(c) for c in checks]
