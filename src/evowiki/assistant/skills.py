"""The reading assistant's Skills (design B2; stage 3 T2/T3): the core tool list, the library under
`src/evowiki/skills/`, the code signals that preload a Skill, and what stays active across turns.

Which Skills a request carries is a function of the session log and this turn's recorded tool
results: `carried` (from the last answer's `skills` entry), `preload(...)` (structured signals only:
the learn_goal intent, attachments, a quoted wrong answer, an open blocks draft, a diagnosis started
or waiting; never keywords),
and this turn's load_skill / read_ref results (`agent.catalog.Activation`). A Skill leaves when its
task ends, as code decides it (`carry`): a turn that neither loaded it nor used one of its tools
drops it, and so does a compaction (`session.fold`)."""

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from evowiki.agent.catalog import (
    LOAD_SKILL,
    READ_REF,
    Activation,
    SkillLibrary,
    library_version,
    load_library,
)

# always offered (when the turn built them): reading, asking, loading, confirming, and make_cards
# (「考考我这页」 on a page without cards must not wait for a load the model may skip; it stays Grounded).
# search_memory /
# read_past_turn are the memory session's (assistant/tools/memory.py, not on master yet); the
# names are listed so they join the core the day they are registered.
CORE: tuple[str, ...] = (
    "search_wiki", "read_page", "read_k", "web_search", "fetch_url", "run_python", "ask_user",
    LOAD_SKILL, READ_REF, "read_goals", "today_plan", "open_review", "read_review_digest", "offer",
    "confirm",
    "record_evidence", "make_cards", "search_memory", "read_past_turn",
    # a one-sentence change: seen behind a Skill, the model asked 「可以吗」 instead (walk 2026-11-04)
    "pause_goal", "set_deadline", "set_goal_limits", "postpone_reviews", "mark_known",
    "report_problem")
# pending: the memory session's tools (assistant/tools/memory.py); a Skill may name them already
PENDING_TOOLS = frozenset({"search_memory", "read_past_turn", "forget_memory"})
S3_TOOLS = (LOAD_SKILL, READ_REF, "offer", "confirm")
# the assistant's Skills (src/evowiki/skills/<key>); write-page is the page writer's, read by
# `agent.skills` (deep.skill), never loaded by the assistant
ASSISTANT_SKILLS = ("remember", "plan-goal", "assess", "edit-wiki", "interact", "sources",
                    "settings", "conflicts")
UNDO = "undo"


def registry() -> frozenset[str]:
    """Every tool name a Skill may name: the built-in assistant tools (harness/tool_names, kept
    equal to what the agents build by its test), ask_user, the S3 tools, the pending ones."""
    from evowiki.harness.tool_names import TOOLS
    return frozenset({*TOOLS["assistant"], "ask_user", *S3_TOOLS, *PENDING_TOOLS})


@lru_cache(maxsize=1)
def library() -> SkillLibrary:
    """The assistant's Skills, checked against the registry (a broken Skill file fails the
    turn: fail closed, never a silent partial catalog)."""
    return load_library(registry=registry(), keys=ASSISTANT_SKILLS)


@dataclass(frozen=True)
class Preload:
    """What code loads before the first model call: Skills (rules in the system prompt) and
    references (bodies in the system prompt, tools registered)."""
    skills: tuple[str, ...] = ()
    refs: tuple[str, ...] = ()


def preload(*, learn_goal: bool, session_attachments: bool, message_attachments: bool,
            wrong_answer: bool, open_blocks: bool, diagnose: bool = False,
            conflicts: bool = False, reteach: bool = False) -> Preload:
    """The Skills code loads before the first model call (B2.3 (a)): structured signals only. A
    session with attachments gets plan-goal too (a goal from the file is the common next step)."""
    keys: list[str] = []
    if learn_goal or session_attachments or message_attachments or open_blocks:
        keys.append("plan-goal")
    if session_attachments or message_attachments:
        keys.append("sources")
    if wrong_answer or diagnose:              # a diagnosis in the chat: plan-goal's and assess's
        keys.append("assess")
    if diagnose and "plan-goal" not in keys:
        keys.insert(0, "plan-goal")
    if conflicts:                             # 「处理」 on the 待确认的记忆 card
        keys.append("conflicts")
    if reteach:                               # 「重讲这一节」 after 标记为不会
        keys.append("edit-wiki")
    return Preload(skills=tuple(keys))


def turn_rules(lib: SkillLibrary, skills: Collection[str], bodies: Collection[str] = (),
               names: Collection[str] = ()) -> str:
    """The rules of `skills` for the system prompt, in catalog order, with the bodies of the
    references in `bodies` and only the names of those in `names` (read in an earlier turn: a
    read_ref gives the body again). A Skill's `turn_rule` reference is never here (it goes in the
    user message of its turn)."""
    out: list[str] = []
    for s in lib.skills:
        refs = [r for r in s.refs.values() if r.name != s.turn_rule]
        shown = [r for r in refs if r.path in bodies]
        named = [r.path for r in refs if r.path in names and r.path not in bodies]
        if s.key not in skills and not shown and not named:
            continue
        body = [s.rules] if s.key in skills and s.rules else []
        body += [r.body for r in shown]
        if named:
            body.append(f"（读过的参考：{'、'.join(named)}；要照着做时用 read_ref 再读一次）")
        out.append(f"【技能·{s.name}】\n" + "\n".join(body))
    return "\n\n".join(out)


def skill_of_tools(lib: SkillLibrary) -> dict[str, tuple[str, ...]]:
    """tool name -> the Skills it comes with."""
    out: dict[str, list[str]] = {}
    for s in lib.skills:
        for t in (*s.tools, *(x for r in s.refs.values() for x in r.tools)):
            out.setdefault(t, []).append(s.key)
    return {k: tuple(v) for k, v in out.items()}


def carry(act: Activation, used: Sequence[str]) -> dict[str, Any]:
    """What the next turn keeps (the `skills` entry of this turn's answer): every active Skill
    this turn loaded, or whose tools it used; the references read of those Skills."""
    owners = skill_of_tools(act.library)
    keep = {k for t in used for k in owners.get(t, ())}
    keep |= set(act.loaded) | {p.partition("/")[0] for p in act.read}
    keys = [k for k in act.skills() if k in keep]
    refs = [p for p in act.refs() if p.partition("/")[0] in keys]
    return {"carry": keys, "refs": refs, "versions": act.library.versions(keys, refs),
            "library": library_version(act.library)}
