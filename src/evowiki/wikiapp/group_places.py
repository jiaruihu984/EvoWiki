"""The top groups' vectors (`group_domains`), and where a new goal's blocks would stand on the fixed map (学点新的). A top group is a
level-2 group, or a level-1 group without one; its text is its name and its members' names. Vectors are cached per process by (embedder
model, text), so a graph read embeds only groups whose text changed."""

from collections.abc import Sequence

import numpy as np

from evowiki.nodes import NodeGraph, place_missing
from evowiki.retrieval.embed import Embedder
from evowiki.wikiapp.wiki_schemas import GroupOut

_CACHE: dict[tuple[str, str], list[float]] = {}
_CACHE_MAX = 4096
MAX_MEMBER_NAMES = 40      # a big group's text: its name and its first members (the embedder truncates anyway)


def top_groups(groups: Sequence[GroupOut]) -> list[GroupOut]:
    return [g for g in groups if g.level == 2 or g.parent is None]


def group_text(g: GroupOut, names: dict[str, str]) -> str:
    members = [names[m] for m in g.members if m in names][:MAX_MEMBER_NAMES]
    return g.name + "：" + "、".join(members)


def _vectors(embedder: Embedder, texts: list[str]) -> np.ndarray:
    tag = f"{embedder.id}/{embedder.model_version}"
    todo = [t for t in dict.fromkeys(texts) if (tag, t) not in _CACHE]
    if todo:
        if len(_CACHE) + len(todo) > _CACHE_MAX:
            _CACHE.clear()
        for t, v in zip(todo, embedder.embed_documents(todo), strict=True):
            _CACHE[(tag, t)] = v
    m = np.array([_CACHE[(tag, t)] for t in texts], dtype=float)
    n = np.linalg.norm(m, axis=1, keepdims=True)
    return m / np.where(n == 0, 1, n)


LINK_COSINE = 0.45       # a block linked to a group or block this close (paraphrase-multilingual-mpnet, measured on the user's map)
LINKS_PER_BLOCK = 2


def _free_corner(pos: dict[str, tuple[float, float]], near: Sequence[tuple[float, float]], w: float, h: float) -> tuple[float, float]:
    """The bottom-left corner of a free w × h room: beside `near`'s points if a side is free, else to the right of the whole map."""
    every = list(pos.values())
    if not every:
        return (0.0, 0.0)
    ps = list(near) or every
    x0, x1 = min(p[0] for p in ps), max(p[0] for p in ps)
    y0, y1 = min(p[1] for p in ps), max(p[1] for p in ps)
    cx, cy = (x0 + x1 - w) / 2, (y0 + y1 - h) / 2
    gap = 0.3
    for rx, ry in ((x1 + gap, cy), (x0 - gap - w, cy), (cx, y1 + gap), (cx, y0 - gap - h)):
        if not any(rx - 0.15 < p[0] < rx + w + 0.15 and ry - 0.15 < p[1] < ry + h + 0.15 for p in every):
            return (rx, ry)
    return (max(p[0] for p in every) + gap, cy)


def places_with_blocks(graph: NodeGraph, groups: Sequence[GroupOut], names: dict[str, str], blocks: Sequence[tuple[str, str, str]],
                       embedder: Embedder) -> tuple[dict[str, tuple[float, float]], list[tuple[str, str]]]:
    """学点新的: where a goal's blocks stand on the map, confirmed or not (`blocks`: id, name, scope): a block the map has stays where it is;
    new ones compactly packed in free space, on the side of what they are most like, no point of the map moving (the map makes room when the goal is
    confirmed: `graph_layout.arrange`). Each block's links: its nearest groups and blocks, at most LINKS_PER_BLOCK, at least LINK_COSINE.
    Nothing is stored."""
    placed = place_missing(graph)
    pos = {r.id: (float(r.x), float(r.y)) for r in placed.nodes}
    tops = [g for g in top_groups(groups) if g.id not in {b for b, _, _ in blocks}]
    ids = [g.id for g in tops] + [b for b, _, _ in blocks]
    texts = [group_text(g, names) for g in tops] + [f"{name}：{scope}" for _, name, scope in blocks]
    v = _vectors(embedder, texts) if texts else np.zeros((0, 1))
    sim = v @ v.T
    new = [b for b, _, _ in blocks if b not in pos]
    places = {b: (round(pos[b][0], 6), round(pos[b][1], 6)) for b, _, _ in blocks if b in pos}
    if new:
        # Share confirmed-map cloud/title measurements and packing. Import locally:
        # graph_layout also uses this module's semantic group helpers.
        from evowiki.wikiapp.graph_layout import _picture

        by_id = {key: name for key, name, _ in blocks}
        room, offsets = _picture("", [(by_id[key], [key], 1) for key in new], NodeGraph())
        w, h = room.w, room.h
        like = sim[len(tops):, :len(tops)].mean(axis=0) if tops else np.zeros(0)
        near = tops[int(np.argmax(like))] if tops else None
        (x0, y0) = _free_corner(pos, [pos[m] for m in near.members if m in pos] if near is not None else [], w, h)
        for key in new:
            dx, dy = offsets[key]
            places[key] = (round(x0 + dx, 6), round(y0 + dy, 6))
    if len(ids) < 2:
        return places, []
    links: list[tuple[str, str]] = []
    for i in range(len(tops), len(ids)):
        near = [j for j in np.argsort(sim[i])[::-1] if j != i and sim[i][j] >= LINK_COSINE][:LINKS_PER_BLOCK]
        links += [(ids[i], ids[int(j)]) for j in near]
    return places, links
