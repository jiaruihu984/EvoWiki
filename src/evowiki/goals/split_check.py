"""The one check of a split's parts (on-demand decomposition, spec 2026-10-28 §2.3), shared by
goals/intake.decompose_one_level (entering a block and every 细分 entrance, wikiapp/node_split.py).

A part is unusable when it is the node being split or one of its ancestors (by id, or by
normalized name). The split is invalid when fewer than 2 distinct usable parts are left — this
also covers every part reusing one and the same node. Code never turns an invalid split into a
leaf: the caller asks the model once more, then fails explicitly and the node stays unexpanded.
Pure functions, no IO."""

from collections.abc import Sequence
from dataclasses import dataclass

from evowiki.plain_text import norm_name


@dataclass(frozen=True)
class Part:
    """One part as the caller resolved it: `key` the node it stands for (an existing id, or the
    caller's own key for a new node), `name` as written."""

    key: str
    name: str


@dataclass(frozen=True)
class Checked:
    usable: tuple[int, ...]          # indices of the usable parts, first of each distinct key
    problem: str | None              # None: the split is valid


def check_split(ancestors: Sequence[tuple[str, str]], parts: Sequence[Part]) -> Checked:
    """`ancestors`: (id, name) of the node being split first, then its ancestors."""
    up_ids = {i for i, _ in ancestors}
    up_names = {norm_name(n) for _, n in ancestors if norm_name(n)}
    usable: list[int] = []
    keys: set[str] = set()
    same: list[str] = []
    for i, p in enumerate(parts):
        if p.key in up_ids or norm_name(p.name) in up_names:
            same.append(p.name)
            continue
        if p.key not in keys:
            keys.add(p.key)
            usable.append(i)
    if len(usable) >= 2:
        return Checked(tuple(usable), None)
    node = ancestors[0][1] if ancestors else ""
    if same:
        why = (f"拆出的部分「{'、'.join(same)}」和「{node}」或它的上级是同一个东西，"
               f"去掉以后只剩 {len(usable)} 个部分")
    elif parts:
        why = f"拆出的部分其实只有 {len(usable)} 个不同的点"
    else:
        why = "没有拆出任何部分"
    return Checked(tuple(usable), f"{why}，至少要 2 个不同的部分")
