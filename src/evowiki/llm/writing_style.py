"""Global prose guidance; output and tool contracts always retain priority."""

DEFAULT_LANGUAGE_STYLE = """直接回答当前问题，保留必要的推导、例子和步骤。术语按读者基础解释。删去没有信息的开场、夸赞、预告、收尾、空泛拔高与客服腔。段落按意思推进，步骤、比较、并列信息才用列表，不强凑三点，不每段加总结。按文体处理具体问题，不机械禁词、禁标点或强行口语化。保留事实、范围、条件、否定、不确定性、因果、来源与立场，不编造信息。只交付结果，不描述润色过程。"""

STYLE_HEADER = "[EvoWiki language style]"
STYLE_END = "[/EvoWiki language style]"

STYLE_BOUNDARY = (
    "Language style applies only to natural-language prose, including prose inside structured "
    "outputs. Follow the task's required output language. Preserve all task requirements, safety "
    "rules, facts, evidence, calculations, identifiers, JSON schemas, output formats, tool names "
    "and tool contracts. These requirements take priority over the style below."
)


def prepend_style(system: str, text: str) -> str:
    if system.startswith(f"{STYLE_HEADER}\n") and f"\n{STYLE_END}" in system:
        return system
    block = f"{STYLE_HEADER}\n{STYLE_BOUNDARY}\n{text}\n{STYLE_END}"
    return f"{block}\n\n{system}" if system else block


# Harness calls use rsi.h.* (improver/triage/judge) and rsi.experience.digest;
# forge.* builds/tests tools and patches; eval.* judges outputs. These internal
# outputs are not learner-facing prose. Product purposes keep style during replay.
_INTERNAL_PURPOSE_PREFIXES = ("rsi.", "forge.", "eval.")


def uses_language_style(purpose: str) -> bool:
    """Choose by the call's purpose, never its account, run, or replay context."""
    return not purpose.startswith(_INTERNAL_PURPOSE_PREFIXES)
