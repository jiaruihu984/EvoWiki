"""Plain-text rules of a stored section text (single writer: the deep writer writes every page; this
module only reads what it wrote). Version-dependent or uncommon claims are wrapped in ⟦待查证：…⟧."""

import re

CUE_MAX = 60
ANSWER_MAX = 200
LOOKUP = re.compile(r"⟦待查证：(.+?)⟧")


def lookups(body: str) -> tuple[str, ...]:
    """The ⟦待查证：…⟧ claims of a body, in order."""
    return tuple(LOOKUP.findall(body))
