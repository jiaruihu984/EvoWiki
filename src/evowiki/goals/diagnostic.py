r"""Single-answer questions as evidence (plan rev 3.1 §2 step 2): shared helpers for every question
producer (the adaptive probe `goals/probe.py`, follow-up checks, review cards) and turning answers
into observations. There is no question generator here any more: the old per-cell generator
(its purpose string is listed in tests/llm_cases/inventory.py REMOVED_PURPOSES) was deleted (user ruling
2026-10-11); the probe decides what to ask. There is no separate verification call either: the
structural checks of `CheckItem` (a stem, 2–8 distinct options, the key in range) make an item
gradable, and a gradable item counts as evidence.

Plain text only (the same rule as page text, goals/text.py): a question's stem and options
must not carry LaTeX markup, because the learner sees them as plain text. `latex_in` is the shared
structural check every producer applies (the probe, follow-up checks, review-card rewrites, the
organizer's check sections): an item with LaTeX is refused like an ungradable one. What counts as
LaTeX (`LATEX`):

- inline math in dollars, pandoc's rule: `$` followed by a non-space, a closing `$` preceded by a
  non-space and not followed by a digit (`$x^2$`, `$\sigma$`); and `$$`. So "$5 和 $10" or
  "$5-$10" (prices) pass;
- the delimiters `\(`…`\)` and `\[`…`\]`;
- a TeX command (`TEX_COMMANDS`: `\frac`, `\sum`, `\alpha`, `\mathbf`, `\cdot`, …) not followed
  by a letter. Other backslashes (`"a\nb"`, `\d+` in a Python or regex question, a Windows path)
  pass;
- TeX grouping after a super- / subscript: `^{` and `_{` (`x^{2}`, `a_{ij}`).

Plain-text math passes: `x^2`, `x_i`, `z = (x − μ) / σ`, `√2`, `Σ`, `∂f/∂x`.

Options are shuffled deterministically (seeded by sha256 of cell + stem, so the same question always
gets the same order and the key is not always first) and the key is remapped."""

import hashlib
import random
import re
from collections.abc import Sequence
from typing import Any, Literal

from evowiki.goals.contracts import CheckItem, Observation

IDK = -1                    # the "I don't know" answer
IDK_STRENGTH = 0.8
TEX_COMMANDS = (
    "frac dfrac tfrac sqrt sum prod int iint oint lim infty partial nabla cdot cdots ldots dots "
    "times div pm mp leq le geq ge neq ne approx equiv sim simeq propto in notin subset subseteq "
    "supset cup cap forall exists emptyset mathbf mathrm mathbb mathcal mathit mathsf boldsymbol "
    "bm vec hat bar tilde dot ddot overline underline overrightarrow text textbf operatorname "
    "left right big Big bigg Bigg begin end log ln exp sin cos tan max min arg argmax argmin det "
    "top mid quad qquad to rightarrow Rightarrow leftarrow Leftarrow leftrightarrow iff implies "
    "binom choose over displaystyle langle rangle lfloor rfloor lceil rceil hbar ell circ ast "
    "perp parallel angle prime alpha beta gamma delta epsilon varepsilon zeta eta theta "
    "vartheta iota kappa lambda mu nu xi pi rho sigma tau upsilon phi varphi chi psi omega Gamma "
    "Delta Theta Lambda Xi Pi Sigma Phi Psi Omega")
LATEX = re.compile(
    r"\$\$"                                     # display math
    r"|\$(?=\S)[^$\n]+(?<=\S)\$(?!\d)"          # inline math (pandoc's rule)
    r"|\\\((?s:.*?)\\\)|\\\[(?s:.*?)\\\]"       # \( … \)  \[ … \]
    r"|\\(?:" + "|".join(TEX_COMMANDS.split()) + r")(?![A-Za-z])"   # \frac \sum \alpha …
    r"|[\^_]\{")                                # x^{2} a_{ij}
LATEX_WHY = "题干或选项用了 LaTeX 记号「{found}」，只用纯文本（例如 x^2、z = (x − μ) / σ）"


def latex_in(*texts: str) -> str | None:
    """The first LaTeX markup (module doc) in any of `texts`, or None: plain text."""
    for t in texts:
        if m := LATEX.search(t):
            return m.group(0)[:40]
    return None


def item_latex(item: CheckItem) -> str | None:
    """`latex_in` over a question's stem and options."""
    return latex_in(item.stem, *item.options)


def _options(raw: Sequence[Any]) -> tuple[str, ...] | None:
    """Options as strings; numbers (not bools) are str()-converted; any other type -> None."""
    if not all(isinstance(o, (str, int, float)) and not isinstance(o, bool) for o in raw):
        return None
    return tuple(str(o) for o in raw)


def _shuffled(item: CheckItem) -> CheckItem:
    """The item with its options in a deterministic order seeded by sha256(cell + newline + stem)
    (never Python's per-process hash()); `answer` follows the key to its new index."""
    digest = hashlib.sha256(f"{item.cell}\n{item.stem}".encode()).digest()
    order = list(range(len(item.options)))
    random.Random(int.from_bytes(digest[:8], "big")).shuffle(order)
    return item.model_copy(update={"options": tuple(item.options[j] for j in order),
                                   "answer": order.index(item.answer)})


def is_idk(o: Observation) -> bool:
    """Whether a test answer on a choice item was 不知道: a miss at IDK_STRENGTH."""
    return o.kind in ("quiz", "check") and o.polarity == "-" and o.strength == IDK_STRENGTH


def answer_observation(item: CheckItem, answer: int, *, kind: Literal["quiz", "check"],
                       evidence_ref: str) -> Observation | None:
    """The user's answer to a verified item as an observation; unverified items never count.
    IDK -> ("-", 0.8); the key -> ("+", 1.0); another option -> ("-", 1.0). kind="quiz" for the
    opening diagnostic, "check" for a page's embedded check."""
    if not item.verified:
        return None
    if answer == IDK:
        polarity, strength = "-", IDK_STRENGTH
    elif answer == item.answer:
        polarity, strength = "+", 1.0
    elif 0 <= answer < len(item.options):
        polarity, strength = "-", 1.0
    else:
        raise ValueError(f"answer {answer} is neither IDK nor an option index "
                         f"(0..{len(item.options) - 1})")
    return Observation(cell=item.cell, polarity=polarity, strength=strength, kind=kind,
                       evidence_ref=evidence_ref)
