"""Pure operations on θ_rep,u (the teaching spec, policy track "B"): apply an RSI change's spec op,
revert it (rollback = a new version), describe it in one Chinese line, and the normalized key that
tells "the same change again" apart from a new one. Used by the harness loop (`harness.round`,
`harness.change_source`, `harness.observe`). (Rendering the spec into writer guidelines is R1's
`teach.render`.)"""

import re

from evowiki.teach.contracts import (
    ParamPatchOp,
    PatchBody,
    RepParams,
    SpecItem,
    TeachingSpec,
    rsi_may_change,
)

PARAM_ZH = {"recap_max_per_page": "每页提醒卡上限", "analogy_level": "类比允许程度",
            "fading_level": "例题渐隐程度", "interactive_bias": "交互偏好"}
SCOPE_ZH = {"any": "所有页面", "concept": "概念教程", "exercise": "练习", "quick": "速查"}

class PatchError(ValueError):
    """The body cannot be applied to this spec (unknown item, cap, out-of-range param)."""


def _item(spec: TeachingSpec, item_id: str | None) -> SpecItem | None:
    return next((i for i in spec.items if i.item_id == item_id), None)


def _param_value(path: str, value: float) -> int:
    if path not in RepParams.model_fields:
        raise PatchError(f"unknown RepParams path {path!r}")
    if value != int(value):
        raise PatchError(f"{path} takes an integer, got {value}")
    return int(value)


def locked(item: SpecItem) -> str | None:
    """Why RSI may not change or remove `item` (rule provenance contract §1.3), None when it may."""
    if item.protected:
        return f"item {item.item_id} is a protected base rule; RSI does not change it"
    if item.authority != "rsi":
        return f"item {item.item_id} is the user's own rule ({item.authority}); RSI does not change it"
    if not rsi_may_change(item):
        return f"item {item.item_id} is held from automatic changes for now"
    return None


def apply(spec: TeachingSpec, body: PatchBody, *, patch_id: str) -> TeachingSpec:
    """The spec after `body` (version unchanged: the registry assigns it). add_item needs the new
    id in the body (the proposer assigns it, so the record knows what to observe and revert). An
    added item is RSI's (origin rsi_h, origin_ref = the change id; `rsi_b` stays readable on items
    the retired RSI-B added); an edit or removal of an
    item that is not RSI's (authority user / pending) or is protected is a PatchError (the same
    check `rules.authority.settle` runs again on the commit). An edit keeps the item's origin and
    bumps its rev."""
    if isinstance(body, ParamPatchOp):
        if body.track != "B":
            raise PatchError("RSI only sets the teaching spec's params (track B)")
        v = _param_value(body.path, body.value)
        try:
            params = RepParams.model_validate({**spec.params.model_dump(), body.path: v})
        except ValueError as e:
            raise PatchError(f"{body.path}={v} out of range") from e
        if params == spec.params:
            raise PatchError(f"{body.path} is already {v}")
        return spec.model_copy(update={"params": params})
    items = list(spec.items)
    if body.op == "add_item":
        if not body.item_id or _item(spec, body.item_id) is not None:
            raise PatchError("add_item needs a new, unused item_id")
        items.append(SpecItem(item_id=body.item_id, text=body.text or "", scope=body.scope,
                              domain=body.domain, origin="rsi_h",
                              authority="rsi", origin_ref=patch_id[:200], rev_by="rsi"))
    else:
        old = _item(spec, body.item_id)
        if old is None:
            raise PatchError(f"no item {body.item_id!r} in spec v{spec.version}")
        if (why := locked(old)) is not None:
            raise PatchError(why)
        idx = items.index(old)
        if body.op == "remove_item":
            del items[idx]
        else:
            if body.text == old.text and body.scope == old.scope and body.domain == old.domain:
                raise PatchError(f"edit leaves item {old.item_id} unchanged")
            items[idx] = SpecItem.model_validate({
                **old.model_dump(), "text": body.text, "scope": body.scope,
                "domain": body.domain, "rev": old.rev + 1, "rev_by": "rsi"})
    return spec.model_copy(update={"items": tuple(items)})


def revert(current: TeachingSpec, before: TeachingSpec, body: PatchBody, *,
           actor: str = "rsi") -> TeachingSpec:
    """Undo `body` on the CURRENT spec (later accepted patches stay), using the pre-patch spec
    `before` for what the touched item / param was. Raises PatchError when the touched item /
    param no longer holds what this patch wrote (undoing would silently drop a later change), or the
    item is no longer RSI's (the user took it over, or it became protected: §1.4) — unless the
    user undoes it themself (`actor` "user")."""
    if isinstance(body, ParamPatchOp):
        old = getattr(before.params, body.path)
        if getattr(current.params, body.path) != int(body.value):
            raise PatchError(f"{body.path} was changed again since this patch")
        return current.model_copy(update={"params": current.params.model_copy(
            update={body.path: old})})
    items = list(current.items)
    now = _item(current, body.item_id)
    if body.op != "remove_item" and (now is None or now.text != body.text
                                     or now.scope != body.scope or now.domain != body.domain):
        raise PatchError(f"item {body.item_id} was changed or removed since this patch")
    if actor != "user" and now is not None and (why := locked(now)) is not None:
        raise PatchError(why)
    if body.op == "add_item":
        items.remove(now)
    elif body.op == "edit_item":
        old = _item(before, body.item_id)
        if old is None:
            raise PatchError(f"item {body.item_id} did not exist before; nothing to restore")
        items[items.index(now)] = old
    else:
        old = _item(before, body.item_id)
        if old is None or now is not None:
            raise PatchError(f"item {body.item_id} cannot be restored")
        pos = [i.item_id for i in before.items].index(old.item_id)
        items.insert(min(pos, len(items)), old)
    return current.model_copy(update={"items": tuple(items)})


def touched(body: PatchBody) -> str:
    """The item id or param path a body acts on (what online observation follows)."""
    return body.path if isinstance(body, ParamPatchOp) else (body.item_id or "")


def _clip(s: str, n: int = 40) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def describe(body: PatchBody, before: TeachingSpec) -> str:
    """One plain-Chinese sentence for the change list: the rule itself, no prefix, no numbers."""
    if isinstance(body, ParamPatchOp):
        now = getattr(before.params, body.path, None)
        way = "" if now is None or body.value == now else (
            "调高了一些" if body.value > now else "调低了一些")
        return f"把「{PARAM_ZH.get(body.path, body.path)}」{way or '调整了'}"
    where = "" if body.scope == "any" else f"（{SCOPE_ZH[body.scope]}）"
    if body.op == "add_item":
        return f"{_clip(body.text or '')}{where}"
    old = _item(before, body.item_id)
    old_text = _clip(old.text) if old else body.item_id
    if body.op == "remove_item":
        return f"去掉了「{old_text}」这条规则"
    return f"把「{old_text}」改成了「{_clip(body.text or '')}」{where}"


def describe_for_model(body: PatchBody, before: TeachingSpec) -> str:
    """The informative description (verbs and values) the proposer / reviewer prompts show."""
    if isinstance(body, ParamPatchOp):
        return (f"调整参数「{PARAM_ZH.get(body.path, body.path)}」："
                f"{getattr(before.params, body.path, '?')} → {int(body.value)}")
    where = "" if body.scope == "any" else f"（{SCOPE_ZH[body.scope]}）"
    if body.op == "add_item":
        return f"新增教学规范{where}：「{_clip(body.text or '')}」"
    old = _item(before, body.item_id)
    old_text = _clip(old.text) if old else body.item_id
    if body.op == "remove_item":
        return f"删除教学规范：「{old_text}」"
    return f"修改教学规范{where}：「{old_text}」→「{_clip(body.text or '')}」"


_NOISE = re.compile(r"[\s，。、；：！？,.;:!?“”\"'「」（）()]+")


def body_key(body: PatchBody) -> str:
    """Normalized identity of a patch body: same key = the same patch proposed again. Item ids of
    add_item are fresh each time, so they are left out; whitespace and punctuation are ignored."""
    if isinstance(body, ParamPatchOp):
        return f"set|{body.track}|{body.path}|{body.value:g}"
    text = _NOISE.sub("", body.text or "").lower()
    if body.op == "remove_item":                      # scope / domain mean nothing here
        return f"remove_item|{body.item_id}"
    target = "" if body.op == "add_item" else body.item_id
    return f"{body.op}|{target}|{text}|{body.scope}|{body.domain or ''}"
