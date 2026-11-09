"""The lines the server writes into the profile from what it already knows. No model call.

- 想学什么 / 学到哪了: his goals and, per goal, pages finished and the next step, exactly as
  `wikiapp.progress` (GET /wiki/progress) says them: one definition of progress.
- 已经会的: K only. Per point, the aspects K holds as 确定会 / 可能会, and the lines he wrote here
  himself that K keeps as his self-reports (`profile.known`: the K note of each, verbatim).
- 还不会的: points with an aspect he got wrong or is shaky on, in plain words. Only K: a chat
  summary (assistant/summary.py) is never mastery or preference evidence — its learning items are
  his own words or answers as one session saw them, and its requests hold for that session only.
- 怎么学比较好: his own teaching-spec rules (stated in the chat or typed, and adopted ones):
  `guidelines.said_rules`; the spec is their one store.

Who he is and what he wants is never derived: only his own words put it there (`note.add_fact`,
his edits)."""

from collections.abc import Mapping
from datetime import datetime

from evowiki.belief.aspects import AspectKView
from evowiki.goals.contracts import Aspect, Band
from evowiki.profile.known import PLAIN, PROFILE_REF
from evowiki.profile.note import GAPS, HOW, KNOWN, WANT, WHERE
from evowiki.wikiapp.guidelines import said_rules
from evowiki.wikiapp.kwrite import concept_name
from evowiki.wikiapp.page_run_store import PageRunRow
from evowiki.wikiapp.progress import progress
from evowiki.wikiapp.repo import Snapshot
from evowiki.wikiapp.wiki_schemas import GoalProgressOut, StepOut

GAPS_MAX = 15
KNOWN_MAX = 30


def _step(s: StepOut, language: str = "zh") -> str:
    """The next step as he would say it ("" = none to name)."""
    if language == "en":
        page = f'“{s.title}”' if s.title else "the next page"
        block = f'“{s.region_name}”' if s.region_name else "the next section"
        return {"read": f"Continue reading {page}", "writing": f"{page} is being written",
                "failed": f"Retry writing {page}", "start_page": f"Start {page}",
                "enter_block": f"Start learning {block}", "diagnosis": "Take the initial assessment"}.get(s.kind, "")
    page, block = f"「{s.title}」" if s.title else "下一页", f"「{s.region_name}」" if s.region_name else "下一块"
    return {"read": f"接着读{page}", "writing": f"{page}正在写", "failed": f"{page}没写成，要重写",
            "start_page": f"开始{page}", "enter_block": f"开始学习{block}", "diagnosis": "先做摸底",
            }.get(s.kind, "")


def _where(g: GoalProgressOut, language: str = "zh") -> str:
    if language == "en":
        if g.state == "done":
            return f"{g.goal}: completed"
        line = (f"{g.goal}: {g.pages_done} of {g.pages_total} pages completed"
                if g.pages_total else f"{g.goal}: not started")
        if g.step.region_name and g.step.kind != "enter_block":
            line += f'; currently in “{g.step.region_name}”'
        nxt = _step(g.step, language)
        return f"{line}; next: {nxt}" if nxt else line
    if g.state == "done":
        return f"{g.goal}：学完了"
    line = f"{g.goal}：{g.pages_total} 页里学完 {g.pages_done} 页" if g.pages_total else f"{g.goal}：还没开始"
    if g.step.region_name and g.step.kind != "enter_block":
        line += f"，现在在「{g.step.region_name}」"
    nxt = _step(g.step)
    return f"{line}，下一步：{nxt}" if nxt else line


EN_ASPECTS = {Aspect.TERM: "definitions", Aspect.PURPOSE: "when to use it",
              Aspect.NOTATION: "formulas and notation", Aspect.PROCEDURE: "calculations",
              Aspect.INTERPRET: "interpreting results", Aspect.MECHANISM: "why it works"}


def derive_view(snap: Snapshot, now: datetime,
                runs: Mapping[str, PageRunRow] = {}) -> tuple[dict[str, tuple[str, ...]], dict[str, dict[str, str]]]:
    """Stable source text and its display projection, computed from the same typed records."""
    state, k = snap.state, snap.k
    kview = AspectKView(k, now)
    gaps: list[tuple[str, str]] = []
    known: list[tuple[str, str]] = []
    for c in k.concept_ids():
        aspects = kview.aspects(c)
        if not aspects:
            continue
        bands = {a: kview.cell(f"{c}.{a.value}").band for a in aspects}
        name = concept_name(state, c, user_id=snap.user_id)
        weak = [a for a, b in bands.items() if b in (Band.GAP, Band.SHAKY)]
        if weak:
            gaps.append((f"{name}：还不清楚{'、'.join(PLAIN[a] for a in weak)}",
                         f"{name}: needs work on {', '.join(EN_ASPECTS[a] for a in weak)}"))
        sure = [a for a, b in bands.items() if b is Band.KNOWN]
        likely = [a for a, b in bands.items() if b is Band.LIKELY]
        parts = [*([f"会{'、'.join(PLAIN[a] for a in sure)}"] if sure else []),
                 *([f"可能会{'、'.join(PLAIN[a] for a in likely)}"] if likely else [])]
        english = [*([f"knows {', '.join(EN_ASPECTS[a] for a in sure)}"] if sure else []),
                   *([f"likely knows {', '.join(EN_ASPECTS[a] for a in likely)}"] if likely else [])]
        if parts:
            known.append((f"{name}：{'；'.join(parts)}", f"{name}: {'; '.join(english)}"))
        known.extend((n.text, n.text) for n in k.notes(c) if n.evidence_ref.startswith(PROFILE_REF))
    gs = progress(state, kview, runs).goals
    records = {WANT: [(g.goal, g.goal) for g in gs],
               HOW: [(i.text, i.text) for i in said_rules(state)],
               WHERE: [(_where(g), _where(g, "en")) for g in gs],
               KNOWN: list(dict.fromkeys(known))[:KNOWN_MAX], GAPS: gaps[:GAPS_MAX]}
    return ({s: tuple(dict.fromkeys(canonical for canonical, _ in rows)) for s, rows in records.items()},
            {s: dict(rows) for s, rows in records.items()})


def derived(snap: Snapshot, now: datetime,
            runs: Mapping[str, PageRunRow] = {}) -> dict[str, tuple[str, ...]]:
    """Canonical view used for rules, K retractions and provenance, independent of locale."""
    return derive_view(snap, now, runs)[0]
