"""The one rough token estimate shared by goals/ (context packing) and llm/ (the cacheable-length
check). A leaf module: imports nothing from evowiki, so both layers may use it."""

import math


def _is_cjk(ch: str) -> bool:
    return "　" <= ch <= "鿿" or "＀" <= ch <= "￯"


def estimate_tokens(text: str) -> int:
    """CJK characters count 1 each; every other character counts 1/4 (rounded up overall).
    Deliberately conservative so a packed context stays under a real tokenizer's count."""
    cjk = sum(1 for ch in text if _is_cjk(ch))
    return cjk + math.ceil((len(text) - cjk) / 4)
