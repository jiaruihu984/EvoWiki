"""The pure core of the teaching-rule write checkpoint (rule provenance contract §1.3 / §1.4;
the full description is `rules.authority`'s). It lives in `teach/` so teach code
(`teach.spec_registry.MemoryRegistry`) can run it without importing the wiki app (teach's import
isolation). `settle_items` takes the decisions as `blocks(item) -> bool` and returns the user
decisions it implies as (item, action) pairs; `rules.authority.settle` turns those into
`RuleDecided` events."""

from collections.abc import Callable, Sequence
from typing import Literal

from evowiki.teach.contracts import RepParams, SpecItem, TeachingSpec, rsi_may_change
from evowiki.teach.spec_ops import PARAM_ZH

Actor = Literal["user", "rsi", "system"]

USER_RULE = "是你自己的规则，自动改动不会改它"
PENDING_RULE = "还在等你确认，自动改动不会改它"
PROTECTED_RULE = "是基本约束，自动改动不会改它"
HELD_RULE = "不让自动改动改它"
DECIDED = "你删除或拒绝过这条规则，自动改动不会把它加回来"
NOT_RSI_ADD = "自动改动只能新增它自己的规则"
NO_AUTHORITY_CHANGE = "自动改动不能改变规则归谁"
BAD_HANDOVER = "规则只能交给你自己，不能交出去"
SYSTEM_ADD = "系统写入只能补记待你确认的规则或写入种子风格包"
SYSTEM_EDIT = "系统写入不能改动已有的规则"
USER_PARAM = "是你自己定的讲解设置，自动改动不会改它"
# what a `system` write may add (contract §7.1 #13 / #23): a script's seed pack (RSI's to tune),
# Memory's backfill through S3's land_preference (waits for the user's confirmation), and a rule
# the assistant drafted without his verified words in this message (§6 decision 1, §7 #5: it
# waits for his confirmation too)
SYSTEM_ADDS = frozenset({("seed", "rsi"), ("memory_backfill", "pending"),
                         ("assistant_drafted", "pending")})


class RuleAuthorityError(ValueError):
    """A spec write the checkpoint refuses; nothing was written. `problems`: one plain line each."""

    def __init__(self, problems: Sequence[str]):
        self.problems = tuple(problems)
        super().__init__("；".join(self.problems))


def _clip(text: str, n: int = 30) -> str:
    t = " ".join(text.split())
    return t if len(t) <= n else t[:n - 1] + "…"


def _content(i: SpecItem) -> tuple:
    return (i.text, i.scope, i.domain)


def _stamp(item: SpecItem, **kw) -> SpecItem:
    return SpecItem.model_validate({**item.model_dump(), **kw})


def _lock(o: SpecItem) -> str | None:
    """Why an automatic change may not touch `o`, None when it may."""
    if o.protected:
        return PROTECTED_RULE
    if o.authority == "user":
        return USER_RULE
    if o.authority == "pending":
        return PENDING_RULE
    if not rsi_may_change(o):
        return HELD_RULE
    return None


DecisionAction = Literal["rejected", "revoked", "reaccepted", "taken_over"]


def _never(item: SpecItem) -> bool:
    return False


def settle_items(base: TeachingSpec, new: TeachingSpec, *, actor: Actor,
                 blocks: Callable[[SpecItem], bool] = _never, undo: bool = False
                 ) -> tuple[TeachingSpec, list[tuple[SpecItem, DecisionAction]]]:
    """(`new` with its provenance stamped, the user decisions it implies), or
    RuleAuthorityError. `blocks(item)`: a decision in force forbids an automatic (re-)add.
    `undo`: the user undoing an RSI change — an item put back to what it was keeps the authority
    it had then (undoing RSI's edit of a default rule leaves it RSI's to tune, not the user's)."""
    problems: list[str] = []
    events: list[tuple[SpecItem, DecisionAction]] = []
    old = {i.item_id: i for i in base.items}
    kept = {i.item_id for i in new.items}
    auto = actor != "user"

    def decide(item: SpecItem, action: DecisionAction) -> None:
        events.append((item, action))

    items: list[SpecItem] = []
    for it in new.items:
        o = old.get(it.item_id)
        if o is None:                                   # added (or restored) by this write
            hit = blocks(it)
            if actor == "system" and (it.origin, it.authority) not in SYSTEM_ADDS:
                problems.append(f"「{_clip(it.text)}」：{SYSTEM_ADD}")
            elif actor == "rsi" and it.authority != "rsi":
                problems.append(f"「{_clip(it.text)}」：{NOT_RSI_ADD}")
            elif auto and hit:
                problems.append(f"「{_clip(it.text)}」：{DECIDED}")
            elif not auto and hit and it.authority != "pending":
                decide(it, "reaccepted")
            # a system write only ever adds new items (seed / backfill): they are the system's
            items.append(_stamp(it, rev_by="system") if actor == "system" else it)
            continue
        fixed = {"origin": o.origin, "origin_ref": o.origin_ref, "protected": o.protected}
        changed = _content(it) != _content(o)
        if actor == "system" and (changed or it.authority != o.authority):
            problems.append(f"「{_clip(o.text)}」：{SYSTEM_EDIT}")
        if auto:
            why = _lock(o)
            if (changed or it.authority != o.authority) and why is not None:
                problems.append(f"「{_clip(o.text)}」{why}")
            elif it.authority != o.authority:
                problems.append(f"「{_clip(o.text)}」：{NO_AUTHORITY_CHANGE}")
            stamp = ({"rev": o.rev + 1, "rev_by": "rsi" if actor == "rsi" else "system"}
                     if changed else {"rev": o.rev, "rev_by": o.rev_by})
            items.append(_stamp(it, **fixed, **stamp, authority=o.authority))
            continue
        if changed:
            owner = it.authority if undo and it.authority != "pending" else "user"
            new_item = _stamp(it, **fixed, rev=o.rev + 1, rev_by="user", authority=owner)
            if o.authority == "pending":
                decide(new_item, "reaccepted")
        else:
            new_item = _stamp(it, **fixed, rev=o.rev, rev_by=o.rev_by)
            if it.authority != o.authority:
                if it.authority == "user" and o.authority == "rsi":
                    decide(new_item, "taken_over")
                elif it.authority == "user" and o.authority == "pending":
                    decide(new_item, "reaccepted")
                else:
                    problems.append(f"「{_clip(o.text)}」：{BAD_HANDOVER}")
        items.append(new_item)
    for o in base.items:
        if o.item_id in kept:
            continue
        if auto:
            why = SYSTEM_EDIT if actor == "system" else _lock(o)
            if why is not None:
                problems.append(f"「{_clip(o.text)}」{why}")
        else:
            decide(o, "rejected" if o.authority == "pending" else "revoked")
    user_params = _settle_params(base, new, actor=actor, undo=undo, problems=problems)
    if problems:
        raise RuleAuthorityError(problems)
    return new.model_copy(update={"items": tuple(items), "user_params": user_params}), events


def _settle_params(base: TeachingSpec, new: TeachingSpec, *, actor: Actor, undo: bool,
                   problems: list[str]) -> tuple[str, ...]:
    """The params part of the checkpoint; returns the stamped `user_params` (always derived from
    `base`: no write sets it directly). actors `system` (a seed style pack, `wikiapp.teach_seed`)
    and `rsi` change none the user set (`base.user_params`): what they set stays RSI's to tune;
    actor `user`: the params they changed join `user_params`, except on
    `undo` (the user undoing an RSI change: what it restores was not set by them)."""
    changed = [p for p in RepParams.model_fields
               if getattr(base.params, p) != getattr(new.params, p)]
    for p in changed:
        if actor != "user" and p in base.user_params:
            problems.append(f"「{PARAM_ZH.get(p, p)}」{USER_PARAM}")
    if actor != "user" or undo:
        return base.user_params
    return tuple(p for p in RepParams.model_fields if p in base.user_params or p in changed)


def check(base: TeachingSpec, new: TeachingSpec, *, actor: Actor,
          blocks: Callable[[SpecItem], bool] = _never) -> TeachingSpec:
    """`settle_items` without the decisions it implies: the stamped spec, or RuleAuthorityError."""
    return settle_items(base, new, actor=actor, blocks=blocks)[0]
