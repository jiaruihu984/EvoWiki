"""The answer's word against the turn's tool results (product constraint 2: never claim to have done
what it did not do). Only a tool result makes a card or changes anything; a sentence like 「已经设好
了」 or 「好，记下了」 in a turn where no tool changed anything is the known "completion claim without
a tool call" failure. The answer goes out as written with a line from code after it, true whatever
the sentence meant, so a false hit costs a superfluous line and the claim never stands alone in the
log, in compaction or in memory.

A claim first gets one re-ask (`nudge`, loop.converse): the live text is taken back, the model is
told what it wrote and may call the tool or rewrite; he sees only the second reply. A second reply
that still claims goes out with the line after it. The re-ask says he never saw the first reply:
without that the re-asks of 2026-11-04 apologised for it (「抱歉，上一条消息里我措辞有误」).

Internal ids in the reply (`plain_ids.leaked_ids`) share the one re-ask; what a second reply still shows
goes out as names, and what has no name with `IDS_LEFT` after it. The answer's log says what was asked
(`reasked`) and what stayed (`claim_flagged`, `ids_left`).

A pattern check, so it is a backstop and misses things: a claim in words the verbs below do not
list, an implied one (「按三句来」 alone), a claim about something other than what the tool in this
turn did (a preference saved, 「设好了上限」 said; a quiz counts as a tool that did something).
Matching is per clause and skips conditionals, questions, negations and second-person clauses."""

import re
from collections.abc import Sequence
from decimal import Decimal

from evowiki.agent.tools import Tool, ToolResult
from evowiki.i18n import tr

# what a finished change is called in the answers logged so far
_VERBS = ("记下|记住|记好|记上|记牢|设好|设置好|设过|改好|改完|改过|弄好|搞定|办好|调好|调整好|删好|删掉|删除|"
          "加好|存好|存下|保存|更新好|更新|安排好|推迟|撤销|撤回|关掉|关闭|暂停|标记|标成|设成|设为|改成|改为|调成|"
          "调为|写进|建好|搭好|生成|做好|取代|替换|(?<!确)定下来|改短|缩短|改写|重写|合并|拆好|拆成|挪到|加进")
# 「目标已经确定下来了」 describes its state; do not match 「定下来」 inside 「确定下来」.
# 「已经把 A 和 B 合成一块」: the 把 says it whatever the verb
_DONE = re.compile(rf"已经?[^，：；]{{0,24}}?(?:{_VERBS})(?!的)|(?:{_VERBS})了(?![点再就])|(?<!们)已经?把")
_PROMISE = re.compile(r"(?:以后|今后|从现在起)(?:我|都|就|会|一律|不再|给你|讲)|下次(?:我|会)|不会(?:替你|帮你)?(?:保存|记下|记住|带到|带过去)")
# a sentence with one of these is not a statement of what happened
_OPEN = re.compile(r"[吗？?]|如果|要是|的话|假如|要不要|是否|告诉我|你说|我可以|不准确")
# a clause with one of these is not either
_NOT_DONE = re.compile(r"没|未|不是|可以|能|想|打算|建议|需要|应该|得先|等你|确认|点一下|才|等于|相当于|"
                       r"你[^，：；]{0,4}(?:记|设|改|标|把|删|撤)|我(?:来|就|先)|会帮|帮你把|让我|这样|说一声|曾|被你")
# 「好，改回来」: the same claim said as an agreement
_ACK = re.compile(r"(?:好的?|行|嗯|OK)[，,](?:改|撤|记|删|设|关|加)")
_DRAFT = re.compile(r"草稿|生成")
_BLOCKQUOTE = re.compile(r"^\s*>.*$", re.MULTILINE)
_SENTENCE = re.compile(r"[^。！？!?\n]+[。！？!?]?")
_CLAUSE = re.compile(r"[，,：:；;（()）—]+")
_MARK = re.compile(r"[*_`#>]")
# a list item is an option or a thing listed, not what happened (「1. 只留这条：以后讲解先给例子」)
_LIST_ITEM = re.compile(r"^\s*(?:\d+[.、)）]|[-*•])\s")
# code, formulas and quoted words are not what the assistant says it did
_QUOTED = re.compile(
    r"```.*?```|\$\$.*?\$\$|\$[^$\n]*\$|「[^」]*」|“[^”]*”|‘[^’]*’|\"[^\"\n]*\""
    r"|(?<![A-Za-z])'(?:[^'\n]|(?<=[A-Za-z])'(?=[A-Za-z]))*'(?![A-Za-z])",
    re.DOTALL)

# 「我查了」: a read this turn did not do (constraint 1). His spending: a figure no tool of this turn gave and he did not say
_LOOKED = re.compile(r"查了|查过|查到|查询了|搜了|搜到|翻了(?:一下)?(?:记录|资料)")
_SPENDING = re.compile(r"你[^。！？\n]{0,12}(?:花了|花费|用了|消费)|(?:本月|这个月|今天|累计|目前|一共)[^。！？\n]{0,8}(?:花了|花费|费用|用量)")
_MONEY = re.compile(r"[$＄€£]\s?(\d+(?:,\d{3})*(?:\.\d+)?)|(\d+(?:,\d{3})*(?:\.\d+)?)\s*(?:美元|美金|元|USD|EUR|GBP|dollars?|euros?|pounds?)" , re.IGNORECASE)

NOTHING_CHANGED = "（这一轮没有改动任何内容。）"
NOT_LOOKED = "（这一轮没有查过这一点。）"
ONLY_DRAFTED = "（这一轮只生成了草稿，确认后才生效。）"


_EN_DONE = re.compile(
    r"\bI(?:['’]ve|\s+have)?\s+(?:(?:already|just|successfully)\s+)?"
    r"(?:saved|remembered|recorded|stored|updated|changed|set|deleted|removed|cancelled|canceled|"
    r"scheduled|paused|disabled|enabled|marked|created|generated|written|rewritten|applied|added|"
    r"merged|replaced|fixed|finished|completed)\b|"
    r"\b(?:it|this|that|the (?:change|setting|rule|page|draft))\s+(?:is|has been|was)\s+"
    r"(?:saved|updated|applied|changed|deleted|created|scheduled|set|done)\b", re.IGNORECASE)
_EN_PROMISE = re.compile(
    r"\b(?:from now on|in future|next time|going forward)\b.{0,35}\bI(?:['’]ll| will)\b|"
    r"\bI(?:['’]ll| will)\s+(?:always remember|remember (?:this|that|your)|keep (?:this|that) in mind)\b", re.IGNORECASE)
_EN_NOT = re.compile(
    r"\b(?:not|never|cannot|can['’]t|could|would|should|might|may|can|need to|want to|plan to|"
    r"if|unless|once|when|before|after you|you (?:have |['’]ve )?(?:saved|changed|set|updated)|"
    r"didn['’]t|haven['’]t|hasn['’]t|isn['’]t|wasn['’]t|won['’]t|don['’]t|doesn['’]t)\b", re.IGNORECASE)
_EN_LOOKED = re.compile(r"\bI(?:['’]ve|\s+have)?\s+(?:(?:already|just)\s+)?"
                        r"(?:checked|searched|looked (?:up|at|through)|read|reviewed|queried)\b", re.IGNORECASE)
_EN_SPENDING = re.compile(r"\b(?:you(?:['’]ve| have)? (?:spent|paid|used)|your (?:spending|usage|cost)|"
                          r"(?:today|this month|so far|in total).{0,24}(?:spent|cost|usage))\b", re.IGNORECASE)
_EN_DRAFT = re.compile(r"\b(?:created|generated|prepared|written|saved)\b.{0,40}\bdraft\b|"
                       r"\bdraft\b.{0,30}\b(?:ready|created|generated|prepared|written|saved)\b",
                       re.IGNORECASE)
_EN_CONDITION = re.compile(r"^\s*(?:if|unless|once|when|provided that)\b", re.IGNORECASE)
_ABBREVIATION = re.compile(r"\b(?:e\.g|i\.e|Mr|Mrs|Dr|vs|etc)\.$", re.IGNORECASE)
_PERIOD = re.compile(r"\.(?=\s+|$)")


def _sentences(text: str):
    clean = _QUOTED.sub(" ", _BLOCKQUOTE.sub("", text))
    # Remove contractions' typographic difference without treating apostrophes as quotes.
    for match in _SENTENCE.finditer(clean):
        raw = match.group(0)
        if _LIST_ITEM.match(raw):
            continue
        start = 0
        for period in _PERIOD.finditer(raw):
            if _ABBREVIATION.search(raw[start:period.end()]):
                continue
            yield raw[start:period.end()]
            start = period.end()
        if raw[start:].strip():
            yield raw[start:]


def _clauses(sentence: str):
    # A sentence-level conditional qualifies its consequent too; a later independent sentence does not.
    if _EN_CONDITION.search(sentence) or re.match(r"\s*(?:如果|要是|假如)", sentence):
        return []
    return re.split(r"[，：:；;（()）—]+|(?<!\d),|,(?!\d)|\b(?:but|however|and then)\b", sentence, flags=re.IGNORECASE)


def claim_in(text: str, *, drafted: bool = False) -> str | None:
    """Pattern backstop for unsupported changes/promises; only tool facts authorize a change."""
    for raw in _sentences(text):
        if _LIST_ITEM.match(raw):
            continue
        sent = _MARK.sub("", raw).strip()
        if "不准确" in sent or (drafted and "确认" in sent):
            continue
        if _ACK.match(sent) and not _NOT_DONE.search(sent):
            return sent
        for clause in _clauses(sent):
            if (_OPEN.search(clause) or _NOT_DONE.search(clause) or _EN_NOT.search(clause)
                    or (drafted and (_DRAFT.search(clause) or _EN_DRAFT.search(clause)))):
                continue
            if (_DONE.search(clause) or _PROMISE.search(clause)
                    or _EN_DONE.search(clause) or _EN_PROMISE.search(clause)):
                return sent
    return None


def _known_amount(amount: str, known: str) -> bool:
    wanted = Decimal(amount.replace(",", ""))
    tokens = re.findall(r"(?<![\w.])\d+(?:,\d{3})*(?:\.\d+)?(?![\w.])", known)
    return any(Decimal(token.replace(",", "")) == wanted for token in tokens)


def unbacked_in(text: str, *, looked: bool, known: str) -> str | None:
    """Unsupported read/spending claims, checked per clause rather than per paragraph."""
    for raw in _sentences(text):
        if _LIST_ITEM.match(raw):
            continue
        sent = _MARK.sub("", raw).strip()
        for clause in _clauses(sent):
            if _OPEN.search(clause) or _NOT_DONE.search(clause) or _EN_NOT.search(clause):
                continue
            if not looked and (_LOOKED.search(clause) or _EN_LOOKED.search(clause)):
                return sent
            if (_SPENDING.search(clause) or _EN_SPENDING.search(clause)) and any(
                    not _known_amount(a or b, known)
                    for a, b in _MONEY.findall(clause)):
                return sent
    return None


class ClaimCheck:
    """One turn's tool results and what its final answer may say."""

    def __init__(self, *, lang: str = "zh") -> None:
        self.lang = lang
        self._done = self._drafted = False
        self.looked = False                  # something was read for this turn (a read tool; recall, notes: loop)
        self._results: list[str] = []
        self.owes = False                    # a tool told it to ask him the diagnosis's next question
        self.flagged: str | None = None      # the sentence that got the line (the turn's log)

    def saw(self, tool: Tool | None, result: ToolResult) -> None:
        if tool is not None and tool.spec.name in ("ask_rough", "ask_check", "skip_diagnosis"):
            self.owes = result.error is None and result.pause is None and bool(result.data.get("next_question_required"))
        if tool is not None and result.error is None:
            self._results.append(result.content)
            self.looked = self.looked or tool.spec.category == "read"
        if tool is None or result.error is not None or tool.spec.category != "action":
            return
        if result.proposal is not None or (result.action is not None
                                           and result.action.status == "proposed"):
            self._drafted = True
        else:
            self._done = True

    def claim(self, text: str) -> str | None:
        """The sentence of `text` no tool of this turn backs, or None."""
        return None if self._done else claim_in(text, drafted=self._drafted)

    def unbacked(self, text: str, words: str) -> str | None:
        """The sentence of `text` nothing read this turn backs, or None. `words`: what he said."""
        return unbacked_in(text, looked=self.looked, known="\n".join((words, *self._results)))

    @staticmethod
    def unbacked_nudge(said: str) -> str:
        return (f"（系统校验，他看不到这句，也没看到你上一版回复）上一版写了「{said}」，但这一轮的工具结果里没有这个。"
                "要说就现在调工具查；不查就重写整段回复，只说实际情况。不要提这次校验，不要道歉。")

    def nudge(self, claim: str) -> str:
        """The re-ask's message (loop.converse): never shown, and he never saw the first reply."""
        what = "这一轮只生成了草稿，还没有生效" if self._drafted else "这一轮没有调用任何会改东西的工具，什么也没改"
        return (f"（系统校验，他看不到这句，也没看到你上一版回复）上一版写了「{claim}」，但{what}。"
                "要做就现在调用对应的工具；不该做或做不了，就重写整段回复，只说实际情况。不要提这次校验，不要道歉。")

    IDS_LEFT = "（回答里个别编号是系统内部的，可以不用管。）"

    @staticmethod
    def ids_nudge(ids: Sequence[str]) -> str:
        """The re-ask's message for internal ids in the reply: names, or literal content marked as such."""
        return (f"（系统校验，他看不到这句，也没看到你上一版回复）上一版写了内部编号或工具名 {'、'.join(ids)}，他看不懂。"
                "重写整段回复，页面、小节、知识点、卡片都用名字说；确实是内容里的字面符号，就放进代码块或公式。"
                "不要提这次校验，不要道歉。")

    ASK_OWED = ("（系统校验，他看不到这句，也没看到你上一版回复）摸底还没结束，工具结果里下一步要问他，你没有问，他没有卡片可以答。"
                "回答里要说的话照写，然后调用那个工具。不要提这次校验。")

    def answer(self, text: str, words: str = "") -> str:
        """The text the answer goes out with."""
        claim = self.claim(text)
        if claim is None:
            said = self.unbacked(text, words)
            if said is None:
                return text
            self.flagged = said
            return f"{text}\n\n{tr('(This was not checked in this turn.)', lang=self.lang)}"
        self.flagged = claim
        message = ("(Only a draft was created in this turn. It takes effect after confirmation.)"
                   if self._drafted else "(Nothing was changed in this turn.)")
        return f"{text}\n\n{tr(message, lang=self.lang)}"
