"""The three rewrite knobs of 「再讲深一点 / 换个讲法」 (the product specification
二, vocabulary borrowed from small-sample studies of how explanations are adapted, paraphrased):

  步骤粒度        how much one step does: split finer / merge into bigger steps
  讲解深度        how much derivation, edge cases and meaning of the numbers is given
  假设的已有知识  what the text takes as known: fill in prerequisites below (by K) / leave out what he knows

One source of truth for the page writer's feedback-mode instruction (`feedback_instruction`,
built by `wikiapp.kchange` for `deep.agent`). `deeper` = finer steps + more depth + assume less;
`other` = keep all three, change only the angle. Pure data, no imports (deep/ and wikiapp/ may
import it without a cycle)."""

from typing import Literal

RewriteKind = Literal["deeper", "other"]

KNOB_MEANING: dict[str, str] = {
    "步骤粒度": "一步讲多少：拆得更细，或合并成大步",
    "讲解深度": "推导、边界情况、数值含义讲多少",
    "假设的已有知识": "按 K 往下补前置知识，或往上省略他已经会的",
}

REWRITE_KNOBS: dict[str, dict[str, str]] = {
    "deeper": {
        "步骤粒度": "拆得更细：每一步只做一件事，原来跳过的中间步骤都写出来",
        "讲解深度": "更深：补上推导、边界情况和数值的具体含义",
        "假设的已有知识": "假设他会得更少：按 K 往下补上这一段用到、他还没把握的前置知识",
    },
    "other": {
        "步骤粒度": "保持现在的粒度",
        "讲解深度": "保持现在的深度",
        "假设的已有知识": "保持现在的假设",
    },
}

USER_ASKED = {"deeper": "再讲深一点", "other": "换个讲法"}
ANGLE = ("换一个切入角度（例如从数值例子入手、从反例入手或从几何直观入手），不要重复现在的写法")


def knob_lines(kind: RewriteKind) -> str:
    """The three knobs and their setting for this rewrite, one line each."""
    return "\n".join(f"- {name}（{KNOB_MEANING[name]}）：{setting}"
                     for name, setting in REWRITE_KNOBS[kind].items())


def rewrite_rule(kind: RewriteKind) -> str:
    """What the rewrite changes, in the knobs' words (no leading newline)."""
    if kind == "deeper":
        return (f"用户点了“{USER_ASKED[kind]}”：比之前讲得更深。这次改写的三个旋钮：\n"
                + knob_lines(kind))
    return (f"用户点了“{USER_ASKED[kind]}”：{ANGLE}。三个改写旋钮都保持现在的设置，"
            "只换角度：\n" + knob_lines(kind))


def feedback_instruction(kind: RewriteKind, heading: str) -> str:
    """The page writer's instruction for a 「再讲深一点 / 换个讲法」 rewrite of section `heading`."""
    insert = "（需要时可以在它后面插入新段）" if kind == "deeper" else ""
    return (f"学习者在「{heading}」这一段点了「{USER_ASKED[kind]}」：只改写这一段{insert}；"
            f"现在的写法在页面里，可以用 read_page 看。\n{rewrite_rule(kind)}")
