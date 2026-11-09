"""Harness changes in the RSI change list (track "H", ids prefixed "h_"; design 2026-10-21 §7).

`list`: the user's applied / undone / rolled-back `harness_change` rows as `ChangeOut`: one plain line
(one part per changed key, `summary`: what changed, and for a number / tier key what it was; every H4
and H10 key also what one replayed section cost more or less, the mean of the round's replays), the
why behind the ? tooltip, and the outcome series of its `harness_observation` points (`layer2.outcomes.effect_line`, as track A's).
`undo_change`: restores every key's stored `before` (`store.harness.set_value`, source "rollback") and
the teaching-spec ops through the spec registry (`teach.spec_ops.revert` + `SpecRegistry.commit(undo=True)`,
origin "harness:<round id>" as the apply's), then settles the row. The spec commit is its last step, so
an error before it leaves nothing changed once the caller's transaction rolls back. The user's undo
settles `undone` and writes a `harmful` lesson (`_undo_lesson`), so the improver learns the user
said no; the observation's rollback (`status="rolled_back"`) reuses it. Refused (ValueError,
plain Chinese) when a newer applied change touched the same key, when the key's value is no longer what
the change set, or when a spec rule was changed again since. A forged tool the change promoted (an
H12.tool value, harness 3b Task 7) is rolled back in the forge registry first (`forging.undo_tool` on
the registry version the pack's value carries, on the caller's connection and transaction); a value
without one is refused (forging.UNDO_GONE) before anything is written. The pack lock is taken first.

`HarnessForgeFilter` wraps a forge change source: the forge rows of promotes a harness round made
are hidden and their undo refused (FORGE_ROW): the "h_" row is the one the user sees and undoes.

`mark_rule_pages`: after a change with teaching-spec ops is applied, approved, undone or rolled
back, the pages it left out of date are marked for a lazy rewrite (`wikiapp.rule_stale`, the ops
read back from the round's change row by `spec_ops_of`); a failure is logged, never undoing the
change."""

import logging
import re
import uuid
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any, Literal

from psycopg_pool import ConnectionPool

from evowiki.harness import forge_entries, forging, tool_labels
from evowiki.harness.diff import PackDiff, diff_from_json, h12_tool
from evowiki.harness.keys import h12_tool_key, is_forged_opt_in, key_of
from evowiki.i18n import current_language
from evowiki.llm.prompt_steps import STEP_BY_KEY
from evowiki.rules import authority
from evowiki.store import harness as store
from evowiki.store import harness_rsi
from evowiki.store.db import fetch_all, fetch_one
from evowiki.teach import spec_ops
from evowiki.teach.contracts import (
    HARNESS_REF,
    AnchorDelta,
    ChangeOut,
    OutcomeOut,
    ParamPatchOp,
    PatchBody,
    PatchStatus,
)
from evowiki.teach.layer2.outcomes import NOTE_ROLLBACK_BLOCKED, UNIT_B, effect_line
from evowiki.teach.render import param_effect
from evowiki.teach.spec_registry import SpecRegistry

log = logging.getLogger("evowiki.harness.change_source")

PREFIX = "h_"
TARGET = "运行设置"
LIMIT = 200
STATUS: dict[str, PatchStatus] = {"applied": "personal_accepted", "undone": "rolled_back",
                                  "rolled_back": "rolled_back"}
UNDO_WHY = "撤销了这次改动"
NEWER = "这次改动之后，同一个设置又被改过了，要先撤销后面那次"
ROLLBACK_LOCKED = "这次改动碰到的讲解规则现在是你的了，不能自动撤回"
ROLLBACK_REFUSED = "rollback_refused"     # observation note: confirmed harmful, its undo was refused
FORGE_ROW = "这次改动在运行设置的改动里撤销"
SOURCE_DELETED = "来源会话已删除，这次改动不能撤销"
MAX_SHOWN_POINTS = 20


def _cost_note(before: float | None, after: float | None) -> str:
    """The cost change of the round's replays: the mean cost of one replayed section, reference vs
    composed (not a whole page)."""
    if before is None or after is None:
        return ""
    if current_language() == "en":
        return f"; mean replay cost per section ${before:.3f} → ${after:.3f}"
    d = after - before
    if abs(d) < 0.0005:
        return "，重放一节的花费差不多"
    return f"，重放一节平均约{'多' if d > 0 else '少'}花 ${abs(d):.3f}"


PATCH_LINE = "修改了代码"


def _shown(v, name: str = "") -> str:
    if v is True:
        return "开"
    if v is False:
        return "关"
    if name.startswith("H10.") and name.endswith(".tier"):
        return tool_labels.tier(v)
    return str(v)


def component_name(name: str, titles: Mapping[str, str] | None) -> str:
    """An H8 component key's component by its library name (`load_titles`), else 一个交互组件."""
    ref = tool_labels.component_ref(name.split(".")[2])
    return (titles or {}).get(ref) or tool_labels.COMPONENT


def text_doc(name: str, doc: str, titles: Mapping[str, str] | None) -> str:
    """What a text key is, in plain words: an H8 component note names its component."""
    if name.startswith("H8.component."):
        return f"交互组件「{component_name(name, titles)}」什么时候用"
    if name.startswith("H2."):
        _, agent, tool, *_ = name.split(".")
        act = tool_labels.ACTIONS.get(agent, {}).get(tool)
        return f"{tool_labels.who(agent)}「{act}」这个工具的说明" if act else doc
    return doc


_KEY_NAME = re.compile(r" ?`?(?<![A-Za-z0-9_.])(H\d{1,2}(?:\.[A-Za-z0-9_-]+)+)`? ?")


def _key_words(name: str) -> str:
    k = key_of(name)
    if k is None:
        return "一个设置"
    step = STEP_BY_KEY.get(name[3:]) if k.dim == "H1" else None
    return f"『{step.name}』的写法" if step else text_doc(name, k.doc, None)


def plain_names(text: str) -> str:
    """`text` with every setting's internal name (H2.assistant.x) in plain words."""
    return _KEY_NAME.sub(lambda m: _key_words(m.group(1)), text)


def _count(v) -> int:
    return len(v) if isinstance(v, list) else 0


def _line(name: str, value, before, cost: str, titles: Mapping[str, str] | None = None) -> str:
    """One op's plain line, never an internal identifier. `cost`: the cost note (H4 / H10 ops);
    `titles` (`load_titles`): candidate id -> title of a forged tool / code patch
    (forge_candidate.spec->>'title'; a patch without one is PATCH_LINE), component_ref(id) -> an
    H8 component's library name."""
    if current_language() == "en":
        return _english_line(name, value, before, titles) + cost
    k = key_of(name)
    if k is None:
        return f"改了设置 {name}"
    parts = name.split(".")
    if k.dim == "H12":
        if parts[1] == "patch":
            title = (titles or {}).get(parts[2])
            return f"{PATCH_LINE}：{title}" if title else PATCH_LINE
        shown = value if value is not None else before
        name_ = (titles or {}).get(shown.get("candidate_id", "")) if isinstance(shown, dict) else None
        name_ = name_ or tool_labels.FORGED_TOOL      # never the identifier
        if value is None:
            return f"去掉了工具「{name_}」"
        does = (titles or {}).get(tool_labels.DOES_REF + str(value.get("candidate_id", "")))
        return f"新增了工具「{name_}」：{does}" if does else f"新增了工具「{name_}」"
    if k.dim == "H3":
        agent, tool = parts[1], parts[2]
        if is_forged_opt_in(name):
            title = (titles or {}).get(tool_labels.forged_ref(agent, tool))
            return tool_labels.forged_line(agent, bool(value), title)
        on = value is not False                   # None: back to the default, the tool is on
        return tool_labels.state_line(agent, tool, on) or f"{tool_labels.who(agent)}改了一个工具的开关"
    if k.kind == "text":
        step = STEP_BY_KEY.get(name[3:]) if k.dim == "H1" else None
        doc = text_doc(name, k.doc, titles)
        what = f"『{step.name}』的写法" if step else doc
        if value is not None:
            return f"改了{what}"
        return f"『{step.name}』的写法改回了默认" if step else f"『{doc}』的写法改回了默认"
    if k.kind == "steps":
        return f"改了{k.doc}（{len(value)} 步）" if value else f"去掉了{k.doc}"
    if k.kind == "list":                          # H8 spec parts
        if value is None:
            return f"{k.doc}改回了默认"
        return f"{k.doc}改为 {tool_labels.spec_parts(value)}"
    if k.kind == "json" and name.startswith("H8.component."):
        return f"交互组件「{component_name(name, titles)}」的预设改为 {_count(value)} 个"
    if k.kind == "json":                          # H8.deep.evoui_examples
        n, b = _count(value), _count(before)
        if n > b:
            return f"加了 {n - b} 个交互块示例"
        return f"交互块示例改为 {n} 个" if n < b else f"改了交互块示例（{n} 个）"
    old = "默认值" if before is None else _shown(before, name)
    new = "默认值" if value is None else _shown(value, name)
    return f"{k.doc}改为 {new}（原来 {old}{cost}）"


def candidate_ids(diffs) -> set[str]:
    """The names the rows of these stored diffs need (`load_titles`): the forge candidates their
    ops name (a forged tool's, in its H12 value or what it was before; a code patch's, in the key),
    as component_ref(id) the H8 components and, as forged_ref(agent, tool), the forged tools whose
    opt-in changed on its own (not folded into an H12 line)."""
    out: set[str] = set()
    for diff in diffs:
        d, before, _ = diff_from_json(diff)
        for o in d.ops:
            if is_forged_opt_in(o.name) and not folded(d, o.name):
                _, agent, tool = o.name.split(".")
                out.add(tool_labels.forged_ref(agent, tool))
            elif o.name.startswith("H8.component."):
                out.add(tool_labels.component_ref(o.name.split(".")[2]))
            elif o.name.startswith("H12.patch."):
                out.add(o.name.split(".")[2])
            elif o.name.startswith("H12.tool."):
                for v in (o.value, before.get(o.name)):
                    if isinstance(v, dict) and v.get("candidate_id"):
                        out.add(str(v["candidate_id"]))
    return out


def load_titles(conn, ids: set[str], user_id: str | None = None) -> dict[str, str]:
    """Candidate id -> the learner-facing title (`forge_candidate.spec->>'title'`),
    component_ref(id) -> the component's library name (`component_library`), and, with `user_id`,
    forged_ref(agent, tool) -> the title of the candidate the user's forge registry has under that
    name now (the user's own entry, else the global one unless the user masked it; a name the
    registry no longer has gets none, and the line falls back to tool_labels.FORGED_TOOL)."""
    ref = tool_labels.component_ref("")
    fref = tool_labels.FORGED_REF
    comps = sorted(i.removeprefix(ref) for i in ids if i.startswith(ref))
    forged = sorted(i for i in ids if i.startswith(fref))
    cands = sorted(i for i in ids if not i.startswith(ref) and not i.startswith(fref))
    out: dict[str, str] = {}
    if forged and user_id is not None:
        entries = forge_entries.entries(conn, user_id)       # the user's masks applied
        by_cid: dict[str, list[str]] = {}
        for i in forged:
            cid = entries.get(i.split(":", 2)[2])
            if cid:
                by_cid.setdefault(cid, []).append(i)
        if by_cid:
            for r in fetch_all(conn, "select id, spec->>'title' as title from forge_candidate "
                                     "where id = any(%s)", (sorted(by_cid),)):
                if r["title"]:
                    out.update(dict.fromkeys(by_cid[r["id"]], r["title"]))
    if cands:
        for r in fetch_all(conn, "select id, spec->>'title' as title, spec->>'does' as does "
                                 "from forge_candidate where id = any(%s)", (cands,)):
            if r["title"]:
                out[r["id"]] = r["title"]
            if r["does"]:
                out[tool_labels.DOES_REF + r["id"]] = r["does"]
    if comps:
        out.update({tool_labels.component_ref(r["id"]): r["name"] for r in fetch_all(
            conn, "select component_id as id, body->>'name' as name from component_library "
                  "where component_id = any(%s)", (comps,)) if r["name"]})
    return out


def before_note(diff: dict, titles: Mapping[str, str] | None = None) -> str:
    """The ? tooltip's sentence for H3 ops: what the tool did before the change (a forged tool by
    its title, `load_titles`' forged_ref key, as `_line`)."""
    d, before, _ = diff_from_json(diff)
    out = []
    for o in d.ops:
        k = key_of(o.name)
        if k is None or k.dim != "H3" or folded(d, o.name):      # a new tool had no before
            continue
        _, agent, tool = o.name.split(".")
        was = before.get(o.name) is not False
        if is_forged_opt_in(o.name):
            title = (titles or {}).get(tool_labels.forged_ref(agent, tool))
            out.append(tool_labels.forged_before(agent, was, title))
        else:
            line = tool_labels.state_line(agent, tool, was)
            if line:
                out.append(line)
    return (("Before: " + "; ".join(out)) if current_language() == "en" else
            f"改之前：{'；'.join(out)}") if out else ""


def folded(d: PackDiff, name: str) -> bool:
    """A forged tool's opt-in that comes with its H12 op: the H12 line says it."""
    if not is_forged_opt_in(name):
        return False
    _, agent, tool = name.split(".")
    return any(o.name == h12_tool_key(agent, tool) for o in d.ops)


def summary(diff: dict, cost_before: float | None, cost_after: float | None,
            titles: Mapping[str, str] | None = None) -> str:
    """The change's one plain line: one part per key op (a forged tool's opt-in folded into its
    tool's line; the cost note on every H4 / H10 op), then the rule ops' count and one part per
    teaching-spec param (set_param)."""
    d, before, _ = diff_from_json(diff)
    cost = _cost_note(cost_before, cost_after)
    parts = [_line(o.name, o.value, before.get(o.name), cost if costed(o.name) else "", titles)
             for o in d.ops if not folded(d, o.name)]
    rules = [s for s in d.spec_ops if not isinstance(s, ParamPatchOp)]
    if rules:
        parts.append(f"Updated {len(rules)} teaching rules" if current_language() == "en"
                     else f"改了 {len(rules)} 条讲解规则")
    parts += [f"Future pages: {param_effect(s.path, int(s.value))}" if current_language() == "en" else
              f"以后的页面：{param_effect(s.path, int(s.value))}"
              for s in d.spec_ops if isinstance(s, ParamPatchOp)]
    return ("; ".join(parts) or "No changes") if current_language() == "en" else "；".join(parts) or "没有改动"


def costed(name: str) -> bool:
    """An H4 or H10 key: its line carries the round's replay cost change."""
    k = key_of(name)
    return k is not None and k.dim in ("H4", "H10")


def _newer_touch(rows: list[harness_rsi.ChangeRow], row: harness_rsi.ChangeRow) -> bool:
    mine = {o.name for o in diff_from_json(row.diff)[0].ops}
    return any(r.change_id != row.change_id and r.status == "applied"
               and (r.created_at, r.change_id) > (row.created_at, row.change_id)
               and mine & {o.name for o in diff_from_json(r.diff)[0].ops} for r in rows)


def _rows(conn, user_id: str) -> list[harness_rsi.ChangeRow]:
    return [harness_rsi.ChangeRow(**r) for r in fetch_all(
        conn, "select * from harness_change where user_id = %s and status in "
              "('applied', 'undone', 'rolled_back') order by created_at desc, change_id limit %s",
        (user_id, LIMIT))]


def points(obs: list[harness_rsi.ObsRow], prefix: str = PREFIX) -> list[OutcomeOut]:
    """The observation points as outcome points, oldest first; a refused rollback reads as one a
    newer change held back (`effect_line`'s NOTE_ROLLBACK_BLOCKED). `prefix`: the row id's."""
    return [OutcomeOut(change_id=prefix + o.change_id, at=o.at, verdict=o.verdict,
                       deltas=tuple(AnchorDelta.model_validate(d) for d in o.deltas),
                       note=NOTE_ROLLBACK_BLOCKED if o.note == ROLLBACK_REFUSED else o.note)
            for o in obs]


def _why(row: harness_rsi.ChangeRow, titles: Mapping[str, str] | None = None) -> str:
    """The round's reason, then (H3 ops) what the tool did before."""
    reason, note = row.reason or "", before_note(row.diff, titles)
    if not note:
        return reason
    return f"{reason}{'' if not reason or reason[-1] in '。！？.!?' else '。'}{note}"


def change_out(row: harness_rsi.ChangeRow, can_undo: bool,
               obs: list[harness_rsi.ObsRow] | None = None,
               titles: Mapping[str, str] | None = None) -> ChangeOut:
    """`obs`: the change's observation points (oldest first); the outcome is the newest one's."""
    pts = points(obs or [])
    status = STATUS[row.status]
    gone = status == "rolled_back"
    if pts:
        line = effect_line(pts[-1], rolled_back=gone, unit=UNIT_B)
    else:
        line = effect_line(OutcomeOut(change_id=PREFIX + row.change_id,
                                      at=row.settled_at or row.created_at, verdict="pending"),
                           rolled_back=gone, unit=UNIT_B)
    return ChangeOut(patch_id=PREFIX + row.change_id, track="H", status=status,
                     summary=summary(row.diff, row.cost_before, row.cost_after, titles),
                     why=_why(row, titles),
                     signals=(), at=row.settled_at or row.created_at,
                     can_undo=can_undo, layer=0, target="Runtime settings" if current_language() == "en" else TARGET, kind="harness",
                     tool_live=row.status == "applied"
                     and h12_tool(diff_from_json(row.diff)[0]) is not None,
                     outcome=pts[-1].verdict if pts else "pending",
                     effect=tuple(pts[-MAX_SHOWN_POINTS:]), effect_line=line)


def outcome_fields(obs: list[harness_rsi.ObsRow], *, prefix: str, rolled_back: bool
                   ) -> dict[str, Any]:
    """ChangeOut's outcome / effect / effect_line from observation points (none: {}), as the h_
    rows carry them; a code patch row (`forgep:`) uses it for its harness observation points."""
    pts = points(obs, prefix)
    if not pts:
        return {}
    return {"outcome": pts[-1].verdict, "effect": tuple(pts[-MAX_SHOWN_POINTS:]),
            "effect_line": effect_line(pts[-1], rolled_back=rolled_back, unit=UNIT_B)}


def _obs(conn, user_id: str, rows: list[harness_rsi.ChangeRow]) -> dict[str, list[harness_rsi.ObsRow]]:
    out: dict[str, list[harness_rsi.ObsRow]] = {}
    if rows:
        for r in fetch_all(conn, "select * from harness_observation where user_id = %s and "
                                 "change_id = any(%s) order by at, seq",
                           (user_id, [x.change_id for x in rows])):
            out.setdefault(r["change_id"], []).append(harness_rsi.ObsRow(**r))
    return out


def _is_tool(op) -> bool:
    return op.name.startswith("H12.tool.") and isinstance(op.value, dict)


def _holds(cur, op) -> bool:
    """The pack still holds what the change set. A forged tool's H12 value is matched on its
    candidate: an approved change stored it before its promote added the registry version."""
    if _is_tool(op):
        return isinstance(cur, dict) and cur.get("candidate_id") == op.value.get("candidate_id")
    return cur == op.value


def _registry_version(cur) -> bool:
    """A forged tool's H12 value records the registry version its promote got (an int)."""
    rv = cur.get("registry_version") if isinstance(cur, dict) else None
    return isinstance(rv, int) and not isinstance(rv, bool)


def _pack_refusal(pack, diff) -> str | None:
    """Why the pack no longer lets `diff` be undone (a later edit of a key it set, or a forged
    tool's promote that cannot be rolled back), else None."""
    for op in diff.ops:
        cur = pack.values.get(op.name)
        if not _holds(cur, op):
            return NEWER
        if _is_tool(op) and not _registry_version(cur):
            return forging.UNDO_GONE               # the promote it made cannot be rolled back
    return None


def _can_undo(pack, rows, r) -> bool:
    """The list's 撤销: what `undo_change` checks before it changes anything (no spec check)."""
    return (r.status == "applied" and not r.diff.get("source_deleted")
            and not _newer_touch(rows, r)
            and _pack_refusal(pack, diff_from_json(r.diff)[0]) is None)


def undo_change(conn, user_id: str, change_id: str, *, registry: SpecRegistry | None,
                status: Literal["undone", "rolled_back"] = "undone", now: datetime) -> None:
    """Undoes the applied harness change `change_id` (the bare id, no "h_"). LookupError for an
    unknown id, ValueError (plain reason) when it cannot be undone. The caller owns the transaction.

    Lock order: the pack lock (`store.lock_key`) first, then (`forging.undo_tool`) the forge
    registry's, the order a round's apply and an approval take them (`round._commit`: pack lock,
    then `promote_for` -> `registry.promote`); the checks below read the pack under it."""
    store.lock_key(conn, user_id)
    row = fetch_one(conn, "select * from harness_change where change_id = %s and user_id = %s",
                    (change_id, user_id))
    if row is None:
        raise LookupError(f"没有这次设置改动：{change_id}")
    row = harness_rsi.ChangeRow(**row)
    if row.status != "applied":
        raise ValueError("这次改动已经不在生效中，不能再撤销")
    if row.diff.get("source_deleted"):        # harness.forget: its quoted words were removed
        raise ValueError(SOURCE_DELETED)
    if _newer_touch(_rows(conn, user_id), row):
        raise ValueError(NEWER)
    diff, before, spec_version = diff_from_json(row.diff)
    pack = store.load_pack(conn, user_id)
    reason = _pack_refusal(pack, diff)
    if reason is not None:
        raise ValueError(reason)
    if diff.spec_ops:
        if registry is None or spec_version is None:
            raise ValueError("讲解规则的旧版本找不到了，不能撤销")
        cur = registry.current(user_id)
        old = registry.get(user_id, spec_version)
        assert cur.spec is not None and old.spec is not None
        spec = cur.spec
        try:
            for op in reversed(diff.spec_ops):
                spec = spec_ops.revert(spec, old.spec, op, actor="user")
            if status == "rolled_back":
                # an automatic rollback only reverts what is still RSI's (rule provenance
                # contract §1.4); checked before anything is written (the commit checks again,
                # with the user's decisions)
                authority.check(cur.spec, spec, actor="rsi")
        except spec_ops.PatchError as e:
            raise ValueError("这次改动之后，讲解规则又被改过了，要先撤销后面那次") from e
        except authority.RuleAuthorityError as e:
            raise ValueError(f"{ROLLBACK_LOCKED}：{e}") from e
    for op in diff.ops:
        if _is_tool(op):
            forging.undo_tool(conn, user_id, pack.values[op.name])
    for op in diff.ops:
        store.set_value(conn, user_id, op.name, before.get(op.name), source="rollback",
                        why=UNDO_WHY, now=now)
    harness_rsi.settle_change(conn, change_id, status, now=now)
    from evowiki.store.bot import sync_rsi_undone
    sync_rsi_undone(conn, user_id)
    if status == "undone":
        _undo_lesson(conn, user_id, row, now)
    if diff.spec_ops:
        # last (its own connection, committed at once): a failure above leaves nothing changed
        assert registry is not None
        registry.commit(user_id, spec, patch_id=f"harness:{row.round_id}", at=now, undo=True,
                        actor="user" if status == "undone" else "rsi")


def _undo_lesson(conn, user_id: str, row: harness_rsi.ChangeRow, now: datetime) -> str:
    """The user undid an applied change: a `harmful` lesson in the experience book, so the improver
    sees it next time (the rollback writes its own in `observe`). Situation and scope as the
    round's lessons: the round's agent and finding (the signal kinds are not stored on the round,
    so they are left empty); general when the improver chose the general candidate."""
    rnd = harness_rsi.get_round(conn, user_id, row.round_id)
    cases = (rnd.cases if rnd is not None else None) or {}
    decision = (rnd.decision if rnd is not None else None) or {}
    situation = {"agent": cases.get("agent", "deep"), "signal_kinds": [],
                 "finding": rnd.finding if rnd is not None else None}
    return harness_rsi.add_experience(
        conn, user_id, situation, row.diff,
        {"change_id": row.change_id, "round_id": row.round_id, "by": "user", "note": UNDO_WHY},
        "harmful", "general" if decision.get("improver") == "a" else "user", weight=1.0, now=now)


CLEAR_TRIES = 3


def clear_marks(repo: Any, new_id: Callable[[str], str], user_id: str, change_id: str,
                now: datetime) -> bool:
    """After an undo / rollback of harness change `change_id` (bare id): drops the rewrite marks
    its apply left (`round.mark_pages`, job "h_<id>"), so those pages get no rewrite under the
    restored pack. One `PageMarksCleared` when any mark is the change's; retried on a conflicting
    commit (the last conflict propagates). True when marks were cleared."""
    from evowiki.wikiapp.events import PageMarksCleared, WikiEvent
    from evowiki.wikiapp.repo import Commit, WikiConflict
    job = PREFIX + change_id
    for attempt in range(CLEAR_TRIES):
        snap = repo.load(user_id)
        if not any(m.job_id == job for m in snap.state.reorder_marks.values()):
            return False
        try:
            repo.commit(snap, Commit(wiki=[WikiEvent(event_id=new_id("wev"), ts=now,
                                                     body=PageMarksCleared(job_id=job))]))
            return True
        except WikiConflict:
            if attempt == CLEAR_TRIES - 1:
                raise
    return False                                                        # pragma: no cover


def spec_ops_of(pool: ConnectionPool) -> Callable[[str, str], tuple[PatchBody, ...] | None]:
    """`wikiapp.rule_stale`'s `ops_of` over the harness tables: the spec ops of the change round
    "harness:<round id>" recorded (None for another origin, or a round with no change)."""
    def ops_of(user_id: str, origin: str) -> tuple[PatchBody, ...] | None:
        if not origin.startswith(HARNESS_REF):
            return None
        with pool.connection() as conn:
            row = harness_rsi.change_of_round(conn, user_id, origin.removeprefix(HARNESS_REF))
        return None if row is None else diff_from_json(row.diff)[0].spec_ops
    return ops_of


def mark_rule_pages(repo: Any, pool: ConnectionPool, new_id: Callable[[str], str], user_id: str,
                    now: datetime) -> int:
    """`wikiapp.rule_stale.mark_stale` for the user (module doc); returns the pages marked, 0 on
    a failure (logged: the change it follows stays as it is)."""
    from evowiki.wikiapp import rule_stale
    try:
        return rule_stale.mark_stale(repo, new_id, user_id, ops_of=spec_ops_of(pool), now=now)
    except Exception:  # broad on purpose: the change is in force; its pages are rewritten later
        log.exception("teaching-rule change of %s: the pages it outdated were not marked", user_id)
        return 0


def _uuid_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


class HarnessChanges:
    """`teach.change_source.ChangeSource` over the harness tables."""

    prefix = PREFIX
    rsi_related = True

    def __init__(self, pool: ConnectionPool, registry: SpecRegistry | None = None,
                 new_id: Callable[[str], str] = _uuid_id):
        self.pool, self.registry, self.new_id = pool, registry, new_id

    def list(self, user_id: str) -> list[ChangeOut]:
        with self.pool.connection() as conn:
            rows = _rows(conn, user_id)
            obs = _obs(conn, user_id, rows)
            titles = load_titles(conn, candidate_ids(r.diff for r in rows), user_id)
            pack = store.load_pack(conn, user_id)
        return [change_out(r, _can_undo(pack, rows, r), obs.get(r.change_id), titles)
                for r in rows]

    def undo(self, user_id: str, change_id: str, *, now: datetime) -> ChangeOut:
        bare = change_id.removeprefix(PREFIX)
        with self.pool.connection() as conn:
            undo_change(conn, user_id, bare, registry=self.registry, now=now)
            rows = _rows(conn, user_id)
            out = next(r for r in rows if r.change_id == bare)
            obs = _obs(conn, user_id, [out])
            titles = load_titles(conn, candidate_ids([out.diff]), user_id)
        from evowiki.wikiapp.repo import PgRepo
        repo = PgRepo(self.pool)
        clear_marks(repo, self.new_id, user_id, bare, now)   # after the undo commits
        if diff_from_json(out.diff)[0].spec_ops:
            mark_rule_pages(repo, self.pool, self.new_id, user_id, now)
        return change_out(out, False, obs.get(bare), titles)


class HarnessForgeFilter:
    """A forge change source (`forge.change_source`) without the rows of promotes a harness round
    made (`forging.harness_promotes`); their undo is refused (FORGE_ROW). Every other attribute
    (the patch source's approve / discard) is the inner source's."""

    def __init__(self, inner: Any, pool: ConnectionPool):
        self.inner, self.pool = inner, pool
        self.prefix = inner.prefix

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def _hidden(self, user_id: str) -> frozenset[str]:
        with self.pool.connection() as conn:
            return forging.harness_promotes(conn, user_id)

    def list(self, user_id: str) -> list[ChangeOut]:
        hidden = self._hidden(user_id)
        return [c for c in self.inner.list(user_id) if c.patch_id not in hidden]

    def undo(self, user_id: str, change_id: str, *, now: datetime) -> ChangeOut:
        if change_id in self._hidden(user_id):
            raise ValueError(FORGE_ROW)
        return self.inner.undo(user_id, change_id, now=now)


_SETTING_LABELS = {
    "max_tool_calls": "Tool calls per turn", "max_rewrites": "Rewrite rounds",
    "max_nudges": "Reminders to call tools", "max_truncated": "Retries after truncated replies",
    "keep_reads": "Read-result retention", "stub_over": "Read-result summarization threshold",
    "context_budget": "Page context budget", "selection_max": "Selected-text limit",
    "paragraph_max": "Paragraph context limit", "top_k_pages": "Related-page count",
    "read_keep_calls": "Read-result retention", "read_stub_over": "Read-result summarization threshold",
    "history_budget": "Conversation compression threshold", "keep_turns": "Recent turns to retain",
    "keep_budget": "Retained conversation budget", "hard_cap": "Conversation hard limit",
    "summary_max_tokens": "Summary length limit", "tier": "Model tier", "thinking": "Reasoning",
    "evoui_examples": "Interactive examples", "before_write": "Steps before writing",
    "before_answer": "Steps before answering", "after_write": "Checks after writing",
    "plan": "Planning guidance", "write": "Writing guidance", "assistant": "Assistant guidance",
    "understand": "Guided-reading guidance", "intake": "Goal intake guidance",
    "path": "Learning-path guidance", "blocks": "Goal-block guidance",
}


def _english_line(name: str, value, before, titles: Mapping[str, str] | None) -> str:
    key = key_of(name)
    if key is None:
        return "Updated runtime settings"
    parts = name.split(".")
    if key.dim == "H12":
        label = (titles or {}).get(parts[-1]) if parts[1] == "patch" else None
        if parts[1] == "patch":
            return "Changed code" + (f": {label}" if label else "")
        entry = value if isinstance(value, dict) else before if isinstance(before, dict) else {}
        title = (titles or {}).get(entry.get("candidate_id", "")) or "a generated tool"
        return ("Removed tool: " if value is None else "Added tool: ") + title
    if key.dim == "H3":
        if is_forged_opt_in(name):
            return tool_labels.forged_line(parts[1], value is not False,
                                           (titles or {}).get(tool_labels.forged_ref(parts[1], parts[2])))
        return tool_labels.state_line(parts[1], parts[2], value is not False) or "Changed tool availability"
    if key.dim == "H2":
        return "Updated tool description: " + (tool_labels.action_label(parts[1], parts[2]) or "Tool")
    if name.startswith("H8.component."):
        title = (titles or {}).get(tool_labels.component_ref(parts[2])) or "interactive component"
        return f"Updated {title} presets ({_count(value)})" if key.kind == "json" else f"Updated usage guidance: {title}"
    label = _SETTING_LABELS.get(parts[-1], "Teaching guidance" if key.dim == "H1" else
                                "Teaching specification" if key.dim == "H8" else "Agent settings")
    if key.kind == "text":
        return ("Restored default: " if value is None else "Updated: ") + label
    if key.kind == "steps":
        return f"Updated {label}: {len(value) if value else 0} steps"
    if key.kind == "list":
        return f"Updated {label}: {tool_labels.spec_parts(value)}"
    if key.kind == "json":
        return f"Updated {label}: {_count(value)} examples"
    def shown(item):
        if item is None:
            return "default"
        if isinstance(item, bool):
            return "on" if item else "off"
        return tool_labels.tier(item) if parts[-1] == "tier" else str(item)
    return f"{label}: {shown(before)} → {shown(value)}"
