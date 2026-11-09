"""Shared prompt guidance for the sentences a learner reads in 最近改了什么 (the why of a change)."""

import re
import unicodedata


def why_rule(*examples: str, field: str = "why") -> str:
    """One prompt line: the why is one plain sentence, shown with the demo's own examples."""
    shown = "".join(f"「{e}」" for e in examples)
    return (f"- {field}（理由）用一句学习者能懂的大白话：不写内部名称、编号、统计数字，"
            f"不用「X：Y」的结构。例如{shown}。")


def norm_name(s: str) -> str:
    """Name-equality key: NFKC (full-width -> half-width), casefold, all whitespace removed; punctuation kept."""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", s).casefold())
