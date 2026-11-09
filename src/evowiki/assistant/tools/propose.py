"""propose_page_edit: draft a page change the USER applies (§9.1 point 3: anything that changes
things only proposes). Validates against the page and returns a `ProposalDraft`; writes nothing.
Accepting goes through the Phase 1 minimal patch (F1 / the proposals route).

Product constraint 3 by construction, not by asking the model to copy: `paragraphs` says which natural
paragraphs of the section the draft is about, and the rest of the section comes from the page, not from
the model (the rule 「其他段一字不改地带上」 failed 1 of 2 in run 110500597858: the last paragraph lost a
sentence, and the answer said it had not moved). A model that hands the whole section back anyway has it
checked against the page. Formulas, links, pictures and blocks may go only when named in `removed`, what he
said to delete; so may the sentences of a section he edited by hand. A refusal names what was lost; the
turn stops the attempts at a cap (loop.CappedTool), and nothing reaches him but a draft that passed."""

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from evowiki.agent.read_page import PINNED, paragraph_spans
from evowiki.agent.text_visuals import has_character_diagram
from evowiki.agent.tool_helpers import (
    fail,
    run_guarded,
    spec,
)
from evowiki.agent.tools import ProposalDraft, ToolContext, ToolResult
from evowiki.goals.contracts import SectionRole, split_cell
from evowiki.goals.guardrails import section_of
from evowiki.wikiapp.page_reader import PageStore

DESCRIPTION = ("提议修改用户的 Wiki 页面：add_section 在某一节后加一节，rewrite_section 重写某一节或其中几段。"
               "不会直接改，返回一张提议卡片给用户看，由用户在卡片上确认或拒绝。用户说「把这个"
               "加进页面」或明显值得记下来时用。why 一句话说明为什么改。")

OK = "已生成修改提议，等用户在卡片上确认（「改写」或「添加」）或拒绝（「暂不改写」或「暂不添加」）。在用户确认前页面不会变。"
_SENTENCE = re.compile(r"[^。！？!?\n]+[。！？!?]?")
DROPPED_SHOWN = 8
_FORMULA = re.compile(r"\$\$.*?\$\$|\$[^$\n]+\$", re.DOTALL)
# what a sentence check cannot see: formulas, links, pictures, a block's or a component's place
_MARK = re.compile(r"\$\$.*?\$\$|\$[^$\n]+\$|!?\[[^\]\n]*\]\([^)\n]*\)|\[\[[^\]\n]*\]\]|\{\{[^}\n]*\}\}", re.DOTALL)
LONG_PARAGRAPH = 12    # chars: a shorter paragraph may stand in other words by chance


def without_heading(text: str, heading: str) -> str:
    """`text` without a first line repeating `heading`: read_page prints a section as `## heading` and
    its body, and a model handing it back would leave the title in the body as well."""
    first, _, rest = text.lstrip().partition("\n")
    return rest.lstrip("\n") if first.lstrip("#").strip() == heading.strip() and first.startswith("#") else text


def _paragraphs(text: str) -> int:
    return sum(1 for p in re.split(r"\n\s*\n", text) if p.strip())


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text)


_SHORTER = re.compile(r"改短|缩短|写短|短一点|精简|简短|\b(?:shorten|shorter|concise|condense|abridge)\b", re.IGNORECASE)


def formulas_gone(old: str, new: str) -> list[str]:
    """The formulas of `old` that `new` does not hold character for character (whitespace aside)."""
    kept = _compact(new)
    return list(dict.fromkeys(f for f in _FORMULA.findall(old) if _compact(f) not in kept))


def scoped_body(old: str, paragraphs: list[int], text: str) -> tuple[str, list[str]]:
    """The section after a draft of `paragraphs` (from 1), and the paragraphs it would lose. `text` holds
    those paragraphs, and the rest of the section comes from `old` byte for byte; a `text` carrying the
    other paragraphs is taken as the whole section and has to carry all of them."""
    spans = paragraph_spans(old)
    chosen = set(paragraphs)
    low, high = min(chosen) - 1, max(chosen) - 1
    body = [old[a:b] for a, b in spans]
    held = _compact(text)
    between = [p for i, p in enumerate(body) if low < i < high and i + 1 not in chosen]
    outside = [p for i, p in enumerate(body) if i < low or i > high]
    whole = any(len(_compact(p)) >= LONG_PARAGRAPH and _compact(p) in held for p in outside)
    lost = [p for p in (*between, *(outside if whole else ())) if _compact(p) not in held]
    if whole or lost:
        return text, lost
    head, tail = old[:spans[low][0]], old[spans[high][1]:]
    if text.strip():
        return head + text.strip() + tail, []
    return (head.rstrip() + ("\n\n" + tail.lstrip() if head.strip() and tail.strip() else tail.lstrip()
                             if not head.strip() else "")), []


def _unmarked(mark: str) -> str:
    return _compact(mark).strip("$")


def marks_gone(old: str, new: str, removed: list[str]) -> list[str]:
    """The formulas, links, pictures and blocks of `old` that `new` lost and `removed` does not name.
    A formula's body counts without its $ marks: $P(k)$ written P(k) is the same formula."""
    kept, named = _compact(new), [_unmarked(r) for r in removed]
    return list(dict.fromkeys(m for m in _MARK.findall(old)
                              if _unmarked(m) not in kept and _unmarked(m) not in named))


def sentences_gone(old: str, new: str, removed: list[str]) -> list[str]:
    kept, named = _compact(new), [_compact(r) for r in removed]
    return [s.strip() for s in _SENTENCE.findall(_MARK.sub("", old))
            if len(_compact(s)) > 1 and _compact(s) not in kept and _compact(s) not in named]


def change_evidence(old: str, new: str) -> str:
    """What the draft does to the section as it stands, counted by code: the answer reports this,
    not its impression. A sentence of the old text missing from the new one may be his own: every
    word on the page counts as his content until he says otherwise."""
    kept = _compact(new)
    formulas = formulas_gone(old, new)
    lost = f"这些公式草稿里没有原样留下：{'、'.join(formulas[:DROPPED_SHOWN])}。回答不能说公式没动。" if formulas else ""
    gone = [s.strip() for s in _SENTENCE.findall(_FORMULA.sub("", old))
            if len(_compact(s)) > 1 and _compact(s) not in kept]
    was, now = len(_compact(old)), len(_compact(new))
    head = (f"这一节原有 {_paragraphs(old)} 个自然段，草稿 {_paragraphs(new)} 个。字数 {was} → {now}，"
            f"回答里的长短照这个数字说。{lost}")
    if not gone:
        return head + "原文的句子都还在。"
    listed = "；".join(f"「{s[:40]}」" for s in gone[:DROPPED_SHOWN])
    more = f"等 {len(gone)} 句" if len(gone) > DROPPED_SHOWN else ""
    return (f"{head}原文里这些句子草稿里没有了：{listed}{more}。回答照这个说删了什么；"
            "页面上的话都算他的内容，别猜它是临时的或多余的。他没要删的，重新提议带上。")


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["add_section", "rewrite_section"]
    page_id: str | None = Field(default=None, min_length=1, max_length=200,
                                description="页面 id，省略=当前页")
    section_id: str | None = Field(
        default=None, min_length=1, max_length=200,
        description="rewrite_section：要重写的节；add_section：插在这节之后，省略=加在页末")
    heading: str = Field(min_length=1, max_length=120)
    text: str = Field(max_length=4000, description="改后的文字：给了几段就只写这几段，不写其他段；整节就写整节。"
                                                  "他要删掉这几段就留空")
    paragraphs: list[int] = Field(
        max_length=50,
        description="rewrite_section：他要改这一节的第几个自然段，从 1 数，空行分段，例如第一段 [1]，其他段由系统原样保留。"
                    "他要改整节、重讲这一节，或者 add_section，填 []")
    removed: list[str] = Field(
        default_factory=list, max_length=60,
        description="他明说要删或要改掉的公式、链接、图，原样抄；他亲手改过的节整节重写时，他明说要删的句子也写这里。没有就不填")
    why: str = Field(min_length=1, max_length=300)
    cue: str = Field(min_length=1, max_length=60,
                     description="这一节的复习题：几天后单独看到也能读懂、能回答，只问一件事，不自带答案")
    answer: str = Field(min_length=1, max_length=200, description="一两句话回答 cue，和 text 一致")
    cells: list[str] = Field(
        default_factory=list, max_length=12,
        description="这节讲到的格子，格式 概念.层面，如 zscore.procedure，只能用本页概念")


class ProposePageEditTool:
    def __init__(self, pages: PageStore):
        self._pages = pages
        self.spec = spec("propose_page_edit", DESCRIPTION, Args, "propose")

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return run_guarded(Args, args, lambda a: self._run(a, ctx))

    def _run(self, a: Args, ctx: ToolContext) -> ToolResult:
        a = a.model_copy(update={"text": without_heading(a.text, a.heading)})
        pid = a.page_id or ctx.page_id
        if pid is None:
            return fail("没有当前页，请给出 page_id（不知道就先用 search_wiki 查）")
        wp = self._pages.get(ctx.user_id, pid)
        if wp is None:
            return fail(f"找不到页面 {pid}")
        ids = {s.id for s in wp.page.outline.sections}
        if a.kind == "rewrite_section" and a.section_id is None:
            return fail("rewrite_section 需要 section_id")
        if a.section_id is not None and a.section_id not in ids:
            return fail(f"页面 {pid} 没有段 {a.section_id}")
        concepts = set(wp.page.outline.concepts)
        for c in a.cells:
            try:
                concept, _ = split_cell(c)
            except ValueError:
                return fail(f"格子 id 不合法：{c}")
            if concept not in concepts:
                return fail(f"格子 {c} 的概念不在《{wp.display_title}》上；要改的是别的页，给那一页的 page_id")
        old = wp.page.texts.get(a.section_id) if a.kind == "rewrite_section" else None
        new = a.text
        if has_character_diagram(new):
            return fail("不要用字符拼示意图。要给这一节配图，请载入 edit-wiki 的 pages，使用 illustrate_section 生成页面修订草稿。")
        if old is not None:
            count = len(paragraph_spans(old.body))
            if bad := [i for i in a.paragraphs if not 1 <= i <= count]:
                return fail(f"这一节只有 {count} 个自然段，没有第 {bad[0]} 段")
            if a.paragraphs:
                new, lost = scoped_body(old.body, a.paragraphs, a.text)
                if lost:
                    shown = "；".join(f"「{p[:40]}」" for p in lost[:DROPPED_SHOWN])
                    return fail(f"他没让动的段落这份草稿里没有了：{shown}。text 只写第 {a.paragraphs} 段，或者把它们原样带上；"
                                "他要改的确实包括它们，就把序号加进 paragraphs。改好再提议一次。")
            if not new.strip():
                return fail("这份草稿会把这一节删空，这个工具不删节。")
            # paragraphs named with no text: deleting them is what the call says
            if (a.text.strip() or not a.paragraphs) and (gone := marks_gone(old.body, new, a.removed)):
                return fail(f"这些公式、链接或图草稿里没有原样留下：{'、'.join(gone[:DROPPED_SHOWN])}。放回去再提议一次；"
                            "他明说要删或要改掉的，原样写进 removed。")
            if wp.page.state(a.section_id).pinned and not a.paragraphs and (
                    gone := sentences_gone(old.body, new, a.removed)):
                listed = "；".join(f"「{s[:40]}」" for s in gone[:DROPPED_SHOWN])
                return fail(f"这一节他亲手改过，这些句子草稿里没有了：{listed}。放回去再提议一次；他明说要删的，原样写进 removed。")
        elif not new.strip():
            return fail("text 不能是空的")
        if (old is not None and any(_SHORTER.search(w) for w in ctx.current_words)
                and len(_compact(new)) >= len(_compact(old.body))):
            return fail(f"他要改短，这份草稿没有变短（原来 {len(_compact(old.body))} 字，草稿 "
                        f"{len(_compact(new))} 字）。删掉一些重写，再提议。")
        if a.kind == "rewrite_section" and section_of(wp.page.outline, a.section_id).role is SectionRole.CHECK:
            return fail("这一节是确认题，不能改写；只改讲解的节")     # the card would only say so at 确认
        draft = ProposalDraft(kind=a.kind, page_id=pid, section_id=a.section_id,
                              heading=a.heading, cells=tuple(dict.fromkeys(a.cells)),
                              text=new, why=a.why, cue=a.cue.strip(), answer=a.answer.strip())
        evidence = change_evidence(old.body, new) if old is not None else ""
        if old is not None and wp.page.state(a.section_id).pinned:
            evidence = f"这一节{PINNED}。{evidence}"
        return ToolResult(content=f"{OK}\n这份草稿改的是《{wp.display_title}》。{evidence}", proposal=draft)
