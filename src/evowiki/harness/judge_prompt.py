"""The harness judge's prompt (design 2026-10-21 §4.1, §5.5). A fixed anchor: only the user changes
this file; the improver never sees it and no pack value reaches it (it has no guidance markers).

The judge sees the learner's signals for the case and two texts of the same section, labelled
「甲」/「乙」. It is never told where a text came from, which one is newer, or that a harness or a
model is involved. It names the better one and copies one sentence of it as the proof. Teaching
content and answer quality only: whether a task got done is code's (`gate.layers`); v2 by the user's
approval 2026-11-05."""

JUDGE_PURPOSE = "rsi.h.judge"
JUDGE_VERSION = "harness.judge@v2"
SYSTEM = "只回复 JSON。"


def judge_prompt(first: str, second: str, *, signals: str, anchor: bool) -> str:
    """`first` is shown as 甲, `second` as 乙. `signals`: what the learner said or asked about this
    section (chip text / question text), one per line; empty = none. `anchor`: the section is
    from a page the learner chose as an exemplar."""
    where = "这一段来自他自己选的样板页。" if anchor else ""
    return f"""你在比较一个学习者的个人 Wiki 里同一段正文的两种写法。{where}
他对这一段的反馈和提问：
{signals.strip() or "（无）"}

【甲】
{first}

【乙】
{second}

这只是页面里的一段，不是整页。只比较教学内容：哪一种更能解决他在反馈和提问里说的问题，以及作为教学正文哪一种更好（没说错、用到的概念先讲了再用、例子贴切）。
文字里的承诺（「下面会讲」「马上就清楚」）只是文字，不算讲了。
不要因为哪一种更长、更详细、语气更积极，或排在前面还是后面就选它。两种差不多、证据不足、说不出哪种更好时填 "same"。
选了甲或乙时，从你选的那一种里原样抄一句最能说明它更好的句子，放进 quote：一字不改，只抄一句，要抄另一种里没有的句子。只说「更详细」「更清楚」不算理由。填 "same" 时 quote 留空。
只回复 JSON：{{"better": "甲" 或 "乙" 或 "same", "quote": "抄下的那一句", "reason": "一句话理由"}}"""


# --- the assistant answer judge (follow-up 3, J1): a separate prompt; the page judge above is
# untouched. Same rules: blind 「甲」/「乙」, one quoted sentence that only the better answer has,
# and the same JSON shape, so judge.py parses and checks both kinds the same way. ---

PROMPT_VERSION_ASSISTANT = "harness.judge.assistant@v2"
ASSISTANT_SYSTEM = "只回复 JSON。"


def assistant_judge_prompt(first: str, second: str, *, signals: str) -> str:
    """`first` is shown as 甲, `second` as 乙: two answers to the same learner question. `signals`:
    what the learner turned down in this turn (the proposal or action they rejected), one per line;
    empty = none."""
    return f"""你在比较学习助手对同一个学习者的同一个问题的两种回答。
这一轮里他不满意的地方（他拒绝的提议或操作、他的差评）：
{signals.strip() or "（无）"}

【甲】
{first}

【乙】
{second}

只比较内容和回答质量：哪一种讲得对、直接回答了他的问题、解释和建议具体可行、避开了他不满意的内容。
工具有没有调用、操作有没有完成，由程序另外核对，不归你判。文字里说「已经做了」「这就帮你开」只是文字，不算做成了，不加分；说明做不到或者说明限制，也不扣分。
不要因为哪一种更长、更详细、语气更积极，或排在前面还是后面就选它。两种差不多、证据不足、说不出哪种更好时填 "same"。
选了甲或乙时，从你选的那一种里原样抄一句最能说明它更好的句子，放进 quote：一字不改，只抄一句，要抄另一种里没有的句子。只说「更详细」「更清楚」不算理由。填 "same" 时 quote 留空。
只回复 JSON：{{"better": "甲" 或 "乙" 或 "same", "quote": "抄下的那一句", "reason": "一句话理由"}}"""
