"""The fixed cases of the conversation-summary evaluator (`evaluation.compact`): the shapes the 8
live compact@v6 outputs on the dev DB got wrong (llm_call purpose='assistant.compact',
prompt_version='compact@v6'; only outputs were stored, so the inputs are rebuilt from them), plus
a long z-score fold (the only one the compression check measures). Protected like the evaluator: only the user changes them.

Each case gives the session to fold, the key points a good summary keeps (the judge checks them),
and code expectations on the reply (`expect`, checked by `check_case`). `case_request` builds the
exact request `assistant.compact` sends, so a rerun with another prompt is evaluated on the same
input. `worth_a_call=False`: the product must not summarise it at all (no request is made)."""

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from evowiki.assistant.compact import compact_request, worth_summarising
from evowiki.assistant.session import SessionInfo, SessionState, Turn
from evowiki.assistant.summary import read_recap, render_handoff
from evowiki.llm.models import LLMRequest

__all__ = ["CASES", "CompactCase", "case_request", "case_state", "check_case", "false_learning",
           "lost_points"]

INFO = SessionInfo(session_id="eval_compact", user_id="eval", page_id="p_z", note_concept_id=None,
                   created_at=datetime(2026, 10, 29, tzinfo=UTC))


@dataclass(frozen=True)
class CompactCase:
    name: str
    turns: tuple[tuple[str, str, str], ...]          # (message id, his text, the answer)
    key_points: tuple[str, ...]
    edits: dict[str, tuple[str, ...]] = field(default_factory=dict)   # accepted, by message id
    previous: dict[str, Any] | None = None            # an earlier compact@v7 payload
    first_kept: int = 0
    # code expectations on the reply: "unclear_empty", "requests_any", "unclear_not_any" (no unclear concept names one of these),
    # "understood_not_any" (likewise: the same message may say he got one point, not another)
    expect: dict[str, Any] = field(default_factory=dict)
    worth_a_call: bool = True
    # which of his messages really say he did not get it / got it, labelled by hand: the code check
    # of what `evidence_id` alone cannot see (that the message exists, not that it supports the
    # claim). Asking is an information need, not learning evidence. A learning item citing any
    # other message is a false one (`false_learning`), the hard metric of the summary.
    unclear_ok: tuple[str, ...] = ()
    understood_ok: tuple[str, ...] = ()
    # what must survive: each entry is words of which the summary must name at least one
    keeps: tuple[tuple[str, ...], ...] = ()


ZCHAT = (("m0", "z 分数是什么意思", "z 分数说一个分数离平均有几个标准差：z = (x − μ)/σ。"),
         ("m1", "考考我", "第 1 题：平均 70、标准差 10，考 85 分的 z 是多少？"),
         ("m2", "1.5", "对，(85 − 70)/10 = 1.5。第 2 题：z = −1.5 说明什么？"),
         ("m3", "好", "那你先想想第 2 题，想好了告诉我。"),
         ("m4", "撤销刚才那个", "刚才没有可撤销的页面修改，第 2 题还等你回答。"))

LONG_FOLD = (
    ("m0",
     ("z 分数到底是什么？课本上写 z = (x − μ)/σ，符号我认识，可是不知道"
     "它想表达什么，为什么要先减再除？"),
     ("可以把它看成换单位。先减去平均值 μ，看这个数比平均高还是低、差了多少分；再除以"
     "标准差 σ，把“差了多少分”换成“差了几个标准差”。这样不管原来的分数是百分制还"
     "是一百五十分制，换算后都在同一把尺子上。举个例子：全班数学平均 70 分，标准差"
     " 10 分，你考了 85 分，那么 z = (85 − 70)/10 = 1.5"
     "，意思是你比平均高出一个半标准差。z 是正的就在平均以上，负的就在平均以下，0 "
     "就正好等于平均。你可以先记住这一句：z 分数回答的是“离平均有几个标准差”。")),
    ("m1",
     "那标准差本身是什么？我只知道它和“分散”有关，具体怎么理解？",
     ("标准差衡量一组数离平均值通常有多远。如果全班分数都挤在 68 到 72 之间，标"
     "准差就小，比平均高 5 分已经很突出；如果分数从 30 到 100 都有，标准差"
     "就大，高 5 分就不算什么。计算上是先求每个数和平均的差，平方后取平均，再开方，"
     "开方是为了让单位回到“分”。所以 z 分数里除以 σ，相当于问：你比平均高的那段"
     "距离，在这个班里算大还是算小。同样高 15 分，在标准差 5 分的班里是 z ="
     " 3，非常少见；在标准差 30 分的班里只是 z = 0.5。")),
    ("m2",
     ("懂了，所以 z = −1 就是比平均低一个标准差，不是低 1 分。那我拿 z 分"
     "数比较语文和数学可以吗？比如语文 z = 1.2，数学 z = 0.8，能说我语"
     "文考得更好吗？我总觉得两门的分数分布不一样，有点不放心。"),
     ("在两门的分布形状差不多（比如都大致对称、接近正态）的时候，可以这样说：语文 z "
     "= 1.2 表示你在语文里比平均高 1.2 个标准差，比数学的 0.8 更靠前。"
     "但如果其中一门的分布很偏，比如数学题太难，大多数人挤在低分、少数人拉得很高，那同"
     "样的 z 分数对应的名次就不一样了，直接比较会误导。这个“分布形状不同时还能不能"
     "比”的问题我们还没讲完，需要看百分位数或者分布图才能判断，你先记着这个疑问，后面"
     "再细说。")),
    ("m3",
     "z 分数能是小数吗？我看到有人算出 z = 2.37 这种。",
     ("可以，z 分数是连续的，算出来是多少就是多少。z = 2.37 表示比平均高出 "
     "2.37 个标准差。取整数只是为了好记，比如常说“超过两个标准差就很少见”。在正"
     "态分布里，大约 68% 的数落在 z = −1 到 1 之间，大约 95% 落在"
     " −2 到 2 之间，所以 z = 2.37 在一个接近正态的班里已经是前 1%"
     " 左右了。小数本身没有任何特别含义，只是更精确地说明了位置。报告成绩时一般保留一"
     "到两位小数就够了，比如 z = 2.37 或 2.4；位数再多也不会让结论更可靠"
     "，因为平均值和标准差本身就是从有限的人数里估出来的，带着误差。")),
    ("m4",
     "如果全班只有一个人比平均高很多，平均值会被拉高吗？这样 z 分数还准吗？",
     ("会被拉高一些，标准差也会变大。一个极端值会同时影响 μ 和 σ：平均往它那边移，"
     "标准差因为多了一段很远的距离而变大。结果是其他人的 z 分数都会被压低一点，那个"
     "极端值自己的 z 分数也不会像你想的那么夸张。所以样本很小或者有明显异常值时，z"
     " 分数要谨慎使用，有时会先检查异常值，或者改用中位数和四分位距这类不太受极端值影"
     "响的量。对一个几十人的班，一两个特别高的分数影响有限，但对只有五六个人的小组就影"
     "响很大。")),
    ("m5",
     "我刚才把页面上“z 分数的意义”那一节改成了用考试排名举例，你看这样讲对吗？",
     ("这样讲是对的，用排名举例很直观：z 分数越大，在班里的位置越靠前。只要补一句提醒"
     "：z 分数说的是离平均有几个标准差，不是名次本身；名次要通过分布才能换算，在接近"
     "正态的时候可以用表格或计算器从 z 查到百分位。你改的那一节已经说清了正负号的含"
     "义，也保留了 z = (x − μ)/σ 的公式，读起来比原来那段纯文字的定义更"
     "容易进入。")),
)

CASES: tuple[CompactCase, ...] = (
    # the one case long enough for the compression check (evaluation.compact.COMPRESSION_MIN_INPUT)
    CompactCase(
        name="long_fold",
        turns=LONG_FOLD,
        key_points=("z 分数是先减平均再除以标准差：把差多少分换成差几个标准差", "标准差衡量离平均通常有多远",
                    "比较语文和数学的 z 分数：两门分布形状差很多时要小心，这是他不放心的点",
                    "极端值会同时影响平均值和标准差", ("他把「z 分数的意义」一节改成用排名举例，助手认可，"
                    "并提醒 z 分数不是名次本身")),
        expect={"understood_not_any": ("跨科目", "比较", "语文", "数学")},
        unclear_ok=("m2",), understood_ok=("m2",)),
    CompactCase(
        name="quiz_left_open",
        turns=ZCHAT,
        key_points=("讲了 z 分数的含义和公式", "第 1 题（求 z）他答对了",
                    "第 2 题（z = −1.5 说明什么）还没答"),
        expect={"unclear_empty": True}, keeps=(("第 2 题", "−1.5", "-1.5"),),
        understood_ok=("m2",)),
    CompactCase(
        name="duplicate_questions_wait",
        turns=(("m0", "摸底的两道题怎么一模一样？我已经说不会了还再问一遍",
                "抱歉，这两道是重复的。你把这两道题发给我，我对照着看是哪里出的问题。"),
               ("m1", "这页没有组件，我喜欢组件多一点的页面",
                "明白了，你更喜欢有交互组件的页面。要我给这一页加一个吗？"),
               ("m2", "先不用", "好，那先这样。")),
        key_points=("摸底题出现了重复，助手在等他提供那两道题", "他喜欢组件多的页面",
                    "这一页先不加组件"),
        expect={"unclear_empty": True, "unclear_not_any": ("重复", "两道题")},
        keeps=(("重复", "一模一样", "相同"), ("组件",), ("喜欢", "偏好", "更想要"), ("先不", "暂不", "暂时不", "不加"))),
    CompactCase(
        name="said_he_understood",
        turns=(("m0", "两个班标准差不一样，同样考 80 分，z 分数会不同吧？",
                "会不同：甲班标准差 5，z = 2；乙班标准差 20，z = 0.5。"),
               ("m1", "嗯，这个我理解了。正态分布那个 68% 和 95% 是怎么来的？需要背吗？",
                "那是曲线下面积：±1 个标准差约 68%，±2 个约 95%，±3 个约 99.7%。记住这三个数就够了。")),
        key_points=("不同班级标准差不同，同分的 z 分数不同，他说理解了", "讲了 68-95-99.7 规则",
                    "这三个数记住就够"),
        expect={"unclear_empty": True},
        understood_ok=("m1",)),
    CompactCase(
        name="draft_not_confirmed",
        turns=(("m0", "这一页没有交互组件，帮我加上，让我直观地能拖动配对",
                "我生成了「拖动配对」的交互草稿：左边一条链，右边拖碱基配对。你确认后才会加进页面。"),
               ("m1", "碱基是什么东西", "碱基是 DNA 上的「字母」，共 4 种：A、T、G、C，A 配 T，G 配 C。")),
        key_points=("碱基是 A、T、G、C 四种，A 配 T、G 配 C",),
        # no field holds a card's state: it is read each turn; words about it in the summary do no harm
        keeps=(("碱基",),)),
    CompactCase(
        name="session_request",
        turns=(("m0", "标准差和方差有什么区别？", "方差是离均值距离平方的平均，标准差是它开根号，单位和原数据一样。"),
               ("m1", "记住以后公式先解释符号", "好，以后给公式前先把每个符号讲清楚。"),
               ("m2", "那 z 分数的公式呢", "先说符号：x 是你的分数，μ 是平均，σ 是标准差。z = (x − μ)/σ。")),
        key_points=("讲了标准差和方差的区别", "他要求给公式前先解释每个符号", "讲了 z 分数的公式"),
        expect={"requests_any": ("符号",), "unclear_empty": True}),
    CompactCase(
        name="resolved_since",
        previous={"version": "compact@v7",
                  "handoff": {"summary": "讲了 z 分数和负的 z。",
                              },
                  "learning": {"unclear": [{"concept": "负的 z 是什么意思", "evidence_id": "m0"},
                                           {"concept": "为什么除以 σ", "evidence_id": "m1"}]},
                  "edits": []},
        first_kept=2,
        turns=(("m0", "负的 z 不懂", "负的 z 在均值以下。"),
               ("m1", "为啥要除以 σ，我还是不懂", "为了换成统一的单位：几个标准差。"),
               ("m2", "第 2 题我选 B，负的 z 我现在懂了",
                "对，B 是比平均低 1.5 个标准差。负号只说方向：在平均以下；数字说离平均有几个标准差。"),
               ("m3", "好的", "好。")),
        key_points=("他答对了第 2 题", "负的 z 他说懂了", "为什么除以 σ 仍没说懂"),
        expect={"unclear_not_any": ("负的 z",)},
        unclear_ok=("m1",), understood_ok=("m2",),
        keeps=(("除以 σ", "除以σ"), ("第 2 题", "第2题", "问题 2"))),
    CompactCase(
        name="greeting_only",
        turns=(("m0", "你好", "你好！想聊哪一页？"),),
        key_points=(),
        worth_a_call=False),
)


def case_state(case: CompactCase) -> SessionState:
    turns = tuple(Turn(message_id=m, user_text=q, selection=None, answer=a,
                       edits=case.edits.get(m, ())) for m, q, a in case.turns)
    recap = read_recap(case.previous) if case.previous else None
    return SessionState(info=INFO, turns=turns, summary=render_handoff(recap),
                        first_kept=case.first_kept, messages=(), decisions={}, recap=recap)


def case_request(case: CompactCase, *, language: str = "zh") -> LLMRequest | None:
    """The request the product sends for `case` (None when it should send none)."""
    st = case_state(case)
    if not worth_summarising(st.turns[st.first_kept:]):
        return None
    return compact_request(st, cut=len(st.turns), language=language)  # type: ignore[arg-type]


def _names(items: Sequence[str], words: Sequence[str]) -> bool:
    return any(w in i for i in items for w in words)


def false_learning(case: CompactCase, raw_output: str) -> list[str]:
    """Learning items citing a message that does not say so (the case's hand labels)."""
    ln = json.loads(raw_output)["learning"]
    return [f"假的 {key}：{e['concept']}（{e['evidence_id']}）"
            for key, ok in (("unclear", case.unclear_ok), ("understood", case.understood_ok))
            for e in ln.get(key, []) if e["evidence_id"].strip() not in ok]


def lost_points(case: CompactCase, raw_output: str) -> list[tuple[str, ...]]:
    """The `keeps` entries the summary names none of."""
    h = json.loads(raw_output)
    text = json.dumps(h, ensure_ascii=False)
    return [words for words in case.keeps if not any(w in text for w in words)]


def check_case(case: CompactCase, raw_output: str) -> list[str]:
    """The case's code expectations a reply misses ([] = all hold; a reply that is not JSON of
    the v7 shape fails here too — `evaluate` says why)."""
    try:
        data = json.loads(raw_output)
        h, ln = data["handoff"], data["learning"]
        unclear = [e["concept"] for e in ln.get("unclear", [])]
        understood = [e["concept"] for e in ln.get("understood", [])]
        requests = list(h.get("active_requests", []))
    except (ValueError, KeyError, TypeError) as exc:
        return [f"not the v7 shape: {exc}"]
    e, out = case.expect, []
    if e.get("unclear_empty") and unclear:
        out.append(f"unclear should be empty: {unclear}")
    if "unclear_not_any" in e and _names(unclear, e["unclear_not_any"]):
        out.append(f"unclear names {e['unclear_not_any']}: {unclear}")
    if "understood_not_any" in e and _names(understood, e["understood_not_any"]):
        out.append(f"understood names {e['understood_not_any']}: {understood}")
    if "requests_any" in e and not _names(requests, e["requests_any"]):
        out.append(f"no active request names {e['requests_any']}: {requests}")
    out += false_learning(case, raw_output)
    out += [f"丢了：{'/'.join(words)}" for words in lost_points(case, raw_output)]
    return out
