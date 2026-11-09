"""The old 读懂 format's wordings of the step 「带你读一篇」 (`H1.understand`, reading-mode spec §7
item 1): the fixed rules that judge a stored wording, shared by scripts/archive_h1_understand.py
(which archives and, with --apply, clears the 不兼容 ones) and the 参数 panel (which tells the learner
when a kept wording is 拿不准: `format_note`). Pure.

The rules (applied to the stored text; the first matching group decides):
  不兼容 ("clear") — the text asks for the old output, a `read` list of sections, one page per
    section:
    - read_field     the field `read` written as a field: `read:` / `read：` / `"read"`, or
                     「read 字段 / 那条 / 这条 / 列表 / 数组」
    - one_per_section 「一节一页」「一节一个」「一节带读一页」「每节一页」「每一节成为一页」, and
                     「每(一)节 … 一页」 within one clause (≤ 12 characters, no 。；)
  拿不准 ("unsure") — it talks about sections and pages or guided reading by section in a way
    the rules cannot place:
    - read_word      the English word `read` elsewhere
    - section_number 「节号」 (the old format wrote section numbers into `read`)
    - section_page   「节」 and 「页」 within one clause (≤ 15 characters, no 。；)
    - guided_section 「带读」 and 「节」 within one clause (≤ 8 characters, no 。；)
  保留 ("keep") — anything else (for example 「前置别列太宽」)."""

import re
from typing import Literal

KEY = "H1.understand"
STEP = "understand"

Verdict = Literal["clear", "unsure", "keep"]
LABEL: dict[Verdict, str] = {"clear": "不兼容", "unsure": "拿不准", "keep": "保留"}

# what the 参数 panel says on the step when its kept wording is 拿不准 (§7 item 1)
FORMAT_NOTE = "这一步的输出格式改了，你以前的写法可能不适用"

_CLAUSE = r"[^。；;\n]"
INCOMPATIBLE: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("read_field", re.compile(
        r"(?<![A-Za-z_])[\"'“”‘’]?read[\"'“”‘’]?\s*[:：]|[\"“”]read[\"“”]"
        r"|(?<![A-Za-z_])read\s*(?:字段|那条|这条|列表|数组|(?:field|list|array)\b)", re.IGNORECASE)),
    ("one_per_section", re.compile(
        rf"一节一页|一节一个|一节带读一页|每一?节{_CLAUSE}{{0,12}}一页|"
        r"\bone page (?:for|per) (?:each |every )?section\b|\beach section.{0,30}\bone page\b", re.IGNORECASE)),
)
UNSURE: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("read_word", re.compile(r"(?<![A-Za-z])read(?![A-Za-z])", re.IGNORECASE)),
    ("section_number", re.compile(r"节号|\bsection numbers?\b", re.IGNORECASE)),
    ("section_page", re.compile(rf"节{_CLAUSE}{{0,15}}页|页{_CLAUSE}{{0,15}}节")),
    ("guided_section", re.compile(rf"带读{_CLAUSE}{{0,8}}节|节{_CLAUSE}{{0,8}}带读")),
)


def classify(text: str) -> tuple[Verdict, tuple[str, ...]]:
    """The verdict on one stored wording and the rules that decided it (module doc)."""
    hit = tuple(n for n, rx in INCOMPATIBLE if rx.search(text))
    if hit:
        return "clear", hit
    hit = tuple(n for n, rx in UNSURE if rx.search(text))
    if hit:
        return "unsure", hit
    return "keep", ()


def format_note(step: str, override: str | None) -> str:
    """The 参数 panel's note on a step: FORMAT_NOTE for `understand` when the learner's own
    wording (`override`; None: the default) is 拿不准 by the rules, else ""."""
    if step != STEP or override is None:
        return ""
    return FORMAT_NOTE if classify(override)[0] == "unsure" else ""
