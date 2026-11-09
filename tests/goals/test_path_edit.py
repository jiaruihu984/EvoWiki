"""User edits of a learning path and replanning that keeps them (goals.path_edit, plan T7)."""

import random

import pytest
from pydantic import TypeAdapter

from evowiki.goals.guardrails import PageConcept
from evowiki.goals.path import LearningPath, PageStatus, PathPage
from evowiki.goals.path_edit import (
    AddEdit,
    Merged,
    MoveEdit,
    PathEdit,
    RemoveEdit,
    SkipEdit,
    WantEdit,
    apply_edits,
    merge_replan,
    own_page,
    user_key,
    visible,
)

PID = "path-x"


def page(i: int, targets: tuple[str, ...], support: tuple[str, ...] = (), *,
         status: PageStatus = PageStatus.NOT_STARTED, pid: str = PID) -> PathPage:
    return PathPage(page_id=f"{pid}-p{i}", targets=targets, concepts=(*support, *targets),
                    objective=f"学完这页你能用 {'、'.join(targets)}", new_cells=1,
                    title="、".join(targets), status=status)


def path(pages: tuple[PathPage, ...], concepts: tuple[str, ...] = ()) -> LearningPath:
    ids = concepts or tuple(dict.fromkeys(c for p in pages for c in p.concepts))
    return LearningPath(path_id=PID, goal="学 X", goal_key="k1", split=len(pages) > 1,
                        source="llm", new_cells=len(pages),
                        concepts=tuple(PageConcept(id=c, name=c.upper()) for c in ids),
                        pages=pages)


class Rec:
    """A stored path (wikiapp.events.PathRecord's fields that merge_replan reads)."""

    def __init__(self, p: LearningPath, page_ids=None, edits=()):
        self.path = p
        self.page_ids = tuple(page_ids) if page_ids is not None else (None,) * len(p.pages)
        self.edits = tuple(edits)


A, B, C, D = page(1, ("a",)), page(2, ("b",), ("a",)), page(3, ("c",), ("b",)), page(4, ("d",))
BASE = (A, B, C, D)


def keys(items) -> list[str]:
    return [i.key for i in visible(items)]


# --- apply_edits -----------------------------------------------------------------------------

def test_no_edits_is_the_stored_order():
    items = apply_edits(BASE, ())
    assert keys(items) == [p.page_id for p in BASE]
    assert not any(i.mine or i.removed or i.skipped or i.moved for i in items)
    assert items[1].node_ids == ("b",) and items[1].page == B


def test_move_is_an_array_move_over_the_shown_list():
    items = apply_edits(BASE, [MoveEdit(page_key=D.page_id, to_index=0)])
    assert keys(items) == ["path-x-p4", "path-x-p1", "path-x-p2", "path-x-p3"]
    assert items[0].moved
    items = apply_edits(BASE, [MoveEdit(page_key=A.page_id, to_index=9)])   # past the end: last
    assert keys(items)[-1] == "path-x-p1"


def test_move_counts_only_shown_pages():
    edits = [RemoveEdit(page_key=A.page_id), MoveEdit(page_key=D.page_id, to_index=1)]
    items = apply_edits(BASE, edits)
    assert keys(items) == ["path-x-p2", "path-x-p4", "path-x-p3"]
    assert [i.key for i in items if i.removed] == ["path-x-p1"]


def test_add_and_want_append_own_pages():
    k1 = user_key("复习一下", None, [])
    k2 = user_key("E", "e", [k1])
    stored = (*BASE, own_page(k2, "e", "E"))
    items = apply_edits(stored, [AddEdit(page_key=k1, title="复习一下"),
                                 WantEdit(page_key=k2, node_id="e", title="E")])
    assert keys(items)[-2:] == [k1, k2]
    own = {i.key: i for i in items if i.mine}
    assert own[k1].page is None and own[k1].node_ids == ()
    assert own[k2].page is not None and own[k2].node_ids == ("e",)
    # a stored own page enters at its add edit, not at its stored position
    items = apply_edits(stored, [WantEdit(page_key=k2, node_id="e", title="E"),
                                 MoveEdit(page_key=k2, to_index=0),
                                 AddEdit(page_key=k1, title="复习一下")])
    assert keys(items) == [k2, *(p.page_id for p in BASE), k1]


def test_skip_needs_every_node_of_the_page():
    two = page(5, ("e", "f"))
    items = apply_edits((A, two), [SkipEdit(page_key=two.page_id, node_ids=("e",))])
    assert not items[1].skipped
    items = apply_edits((A, two), [SkipEdit(page_key=two.page_id, node_ids=("e",)),
                                   SkipEdit(page_key=two.page_id, node_ids=("f",))])
    assert items[1].skipped


def test_edits_on_unknown_keys_are_ignored():
    edits = [MoveEdit(page_key="gone", to_index=0), RemoveEdit(page_key="gone"),
             SkipEdit(page_key="gone", node_ids=("x",))]
    assert keys(apply_edits(BASE, edits)) == [p.page_id for p in BASE]


def test_user_key_is_stable_and_unique():
    k = user_key("复习", "n1", [])
    assert k.startswith("u-") and len(k) == 10 and k == user_key("复习", "n1", [])
    assert user_key("复习", "n1", [k]) == f"{k}-2"


def test_edits_round_trip_as_a_discriminated_union():
    ta = TypeAdapter(tuple[PathEdit, ...])
    edits = (MoveEdit(page_key="a", to_index=1), RemoveEdit(page_key="b"),
             AddEdit(page_key="u-1", title="t"), SkipEdit(page_key="c", node_ids=("n",)),
             WantEdit(page_key="u-2", node_id="n", title="N"))
    assert ta.validate_json(ta.dump_json(edits)) == edits


def _random_edits(rng: random.Random, n: int) -> tuple[list[PathPage], list[PathEdit]]:
    stored = list(BASE)
    edits: list[PathEdit] = []
    for _ in range(n):
        shown = keys(apply_edits(stored, edits))
        op = rng.choice(["move", "remove", "add", "want", "skip"])
        if op == "move" and shown:
            edits.append(MoveEdit(page_key=rng.choice(shown), to_index=rng.randrange(len(shown))))
        elif op == "remove" and shown:
            edits.append(RemoveEdit(page_key=rng.choice(shown)))
        elif op == "add":
            edits.append(AddEdit(page_key=user_key(f"t{len(edits)}", None,
                                                   [i.key for i in apply_edits(stored, edits)]),
                                 title=f"t{len(edits)}"))
        elif op == "want":
            node = f"w{len(edits)}"
            k = user_key(node, node, [i.key for i in apply_edits(stored, edits)])
            stored.append(own_page(k, node, node))
            edits.append(WantEdit(page_key=k, node_id=node, title=node))
        elif op == "skip" and shown:
            edits.append(SkipEdit(page_key=rng.choice(shown), node_ids=(rng.choice("abcd"),)))
    return stored, edits


@pytest.mark.parametrize("seed", range(40))
def test_random_edit_sequences_never_lose_or_duplicate_pages(seed):
    rng = random.Random(seed)
    stored, edits = _random_edits(rng, 25)
    items = apply_edits(stored, edits)
    all_keys = [i.key for i in items]
    added = {e.page_key for e in edits if isinstance(e, AddEdit | WantEdit)}
    assert len(all_keys) == len(set(all_keys))
    assert set(all_keys) == {p.page_id for p in stored} | added


# --- merge_replan ----------------------------------------------------------------------------

def proposal(*pages: PathPage) -> LearningPath:
    return path(tuple(pages))


def merged_keys(m: Merged) -> list[str]:
    return keys(apply_edits(m.path.pages, m.edits))


def test_replan_without_edits_takes_the_proposal_keeping_matching_keys():
    old = Rec(path((A, B, C)))
    new = proposal(page(1, ("a",)), page(2, ("c",), ("b",)), page(3, ("b",), ("a",)))
    m = merge_replan(old, new)
    # b and c keep their old keys (same concepts), order follows the proposal + prerequisites:
    # c needs b, so c goes after b's page
    assert merged_keys(m) == ["path-x-p1", "path-x-p2", "path-x-p3"]
    assert [p.targets for p in m.path.pages] == [("a",), ("b",), ("c",)]
    assert m.edits == () and m.dropped == ()


def test_started_and_done_pages_stay_untouched_in_place():
    a = page(1, ("a",), status=PageStatus.DONE)
    b = page(2, ("b",), ("a",), status=PageStatus.LEARNING)
    old = Rec(path((a, b, C, D)), page_ids=("pg_1", "pg_2", None, None))
    new = proposal(page(1, ("x",)), page(2, ("d",)), page(3, ("c",), ("b",)))
    m = merge_replan(old, new)
    shown = merged_keys(m)
    assert shown[:2] == ["path-x-p1", "path-x-p2"]
    by_key = {p.page_id: p for p in m.path.pages}
    assert by_key["path-x-p1"] == a and by_key["path-x-p2"] == b
    ids = dict(zip((p.page_id for p in m.path.pages), m.page_ids, strict=True))
    assert ids["path-x-p1"] == "pg_1" and ids["path-x-p2"] == "pg_2"
    # the new page x got a fresh key (p1 is taken), never a started page's
    x = next(p for p in m.path.pages if p.targets == ("x",))
    assert x.page_id not in {"path-x-p1", "path-x-p2", "path-x-p3", "path-x-p4"}
    assert set(shown) == {"path-x-p1", "path-x-p2", x.page_id, "path-x-p3", "path-x-p4"}


def test_user_added_pages_stay():
    k = user_key("E", "e", [])
    old = Rec(path((A, B, own_page(k, "e", "E"))),
              edits=[WantEdit(page_key=k, node_id="e", title="E"),
                     AddEdit(page_key="u-note", title="自己的笔记")])
    m = merge_replan(old, proposal(page(1, ("a",)), page(2, ("b",), ("a",)), page(3, ("c",))))
    items = {i.key: i for i in apply_edits(m.path.pages, m.edits)}
    assert items[k].mine and items[k].node_ids == ("e",) and items["u-note"].mine
    shown = merged_keys(m)
    assert shown[:2] == ["path-x-p1", "path-x-p2"] and shown.index(k) < shown.index("u-note")


def test_a_proposal_page_already_taught_by_an_own_page_is_left_out():
    k = user_key("C", "c", [])
    old = Rec(path((A, B, own_page(k, "c", "C"))), edits=[WantEdit(page_key=k, node_id="c",
                                                                    title="C")])
    m = merge_replan(old, proposal(page(1, ("a",)), page(2, ("b",)), page(3, ("c",), ("b",))))
    assert merged_keys(m) == ["path-x-p1", "path-x-p2", k]


def test_removed_page_stays_removed_when_the_proposal_has_its_concepts():
    old = Rec(path(BASE), edits=[RemoveEdit(page_key=C.page_id)])
    m = merge_replan(old, proposal(page(1, ("a",)), page(2, ("b",)), page(3, ("c",)),
                                   page(4, ("d",))))
    assert "path-x-p3" not in merged_keys(m)
    assert [i.key for i in apply_edits(m.path.pages, m.edits) if i.removed] == ["path-x-p3"]
    # a second replan still keeps it removed
    again = merge_replan(Rec(m.path, m.page_ids, m.edits),
                         proposal(page(1, ("c",)), page(2, ("a",))))
    assert "path-x-p3" not in merged_keys(again)


def test_skipped_page_stays_skipped():
    old = Rec(path(BASE), edits=[SkipEdit(page_key=B.page_id, node_ids=("b",))])
    m = merge_replan(old, proposal(page(1, ("a",)), page(2, ("c",)), page(3, ("d",))))
    items = {i.key: i for i in apply_edits(m.path.pages, m.edits)}
    assert items["path-x-p2"].skipped


def test_moved_pages_keep_the_users_relative_order():
    old = Rec(path(BASE), edits=[MoveEdit(page_key=D.page_id, to_index=0),
                                 MoveEdit(page_key=C.page_id, to_index=1)])
    assert keys(apply_edits(BASE, old.edits)) == ["path-x-p4", "path-x-p3", "path-x-p1",
                                                  "path-x-p2"]
    m = merge_replan(old, proposal(page(1, ("a",)), page(2, ("b",)), page(3, ("c",)),
                                   page(4, ("d",)), page(5, ("e",))))
    shown = merged_keys(m)
    assert shown.index("path-x-p4") < shown.index("path-x-p3")
    assert len(shown) == 5


def test_new_pages_go_after_their_prerequisites():
    old = Rec(path((A, B)), edits=[MoveEdit(page_key=B.page_id, to_index=0)])
    # e needs b (moved first by the user) and a
    m = merge_replan(old, proposal(page(1, ("a",)), page(2, ("b",)),
                                   page(3, ("e",), ("a", "b"))))
    shown = merged_keys(m)
    e = next(p.page_id for p in m.path.pages if p.targets == ("e",))
    assert shown.index(e) > shown.index("path-x-p1") and shown.index(e) > shown.index("path-x-p2")


@pytest.mark.parametrize("seed", range(30))
def test_random_edits_survive_a_replan(seed):
    rng = random.Random(seed)
    stored, edits = _random_edits(rng, 12)
    # page 1 started
    ids = [None] * len(stored)
    ids[0] = "pg_started"
    old = Rec(path(tuple(stored)), ids, edits)
    before = apply_edits(old.path.pages, edits)
    new = proposal(*(page(i, (c,)) for i, c in enumerate(rng.sample("abcdxy", 4), 1)))
    m = merge_replan(old, new)
    after = {i.key: i for i in apply_edits(m.path.pages, m.edits)}
    shown = merged_keys(m)
    assert len(shown) == len(set(shown))
    for i in before:
        if i.mine and not i.removed:
            assert i.key in after and after[i.key].mine and not after[i.key].removed
        if i.skipped_nodes and not i.removed:
            assert after[i.key].skipped_nodes == i.skipped_nodes
        if i.removed and i.key in after:
            assert after[i.key].removed
    # the started page is kept with its wiki page id
    wiki = dict(zip((p.page_id for p in m.path.pages), m.page_ids, strict=True))
    assert wiki["path-x-p1"] == "pg_started"
    # user-positioned pages keep their relative order
    pinned = [i.key for i in visible(before) if i.mine or i.moved]
    assert [k for k in shown if k in pinned] == [k for k in pinned if k in shown]
    # replaying the stored edits is what merge computed
    assert [k for k in m.order if not after[k].removed] == shown


def test_a_soft_edge_never_moves_a_page_the_user_moved():
    """Relation layers: a recommended (soft) edge c → a makes the planner propose c's page before
    a's; the user had moved a's page to the front — the user's edit wins over the soft edge."""
    from evowiki.goals.intake import IntakeConcept, IntakeRelation, IntakeResult
    from evowiki.goals.path import DraftPath, order_pages

    goal = IntakeResult(goal="学 X", title="X", targets=("c",), observations=(), dropped=(),
                        resolutions=(), concepts=tuple(IntakeConcept(id=c, name=c.upper())
                                                       for c in "abc"),
                        relations=(IntakeRelation(kind="recommended", src="c", dst="a"),))
    draft = DraftPath.model_validate({"pages": [{"concepts": [c], "objective": "x"}
                                                for c in "abc"]})
    order = order_pages(draft, goal, {c: ("x",) for c in "abc"})
    assert order.order.index(3) < order.order.index(1) and order.blocking == ()   # c before a
    pa, pb, pc = page(1, ("a",)), page(2, ("b",)), page(3, ("c",))
    old = Rec(path((pc, pa, pb)), edits=[MoveEdit(page_key=pa.page_id, to_index=0)])
    assert keys(apply_edits(old.path.pages, old.edits))[0] == pa.page_id
    m = merge_replan(old, proposal(*(dict(enumerate((pa, pb, pc), 1))[i] for i in order.order)))
    shown = merged_keys(m)
    assert shown.index(pa.page_id) < shown.index(pc.page_id)       # a stays before c


# --- the Bot's edits (B5b): moves / adds the Bot made can be re-ordered by a replan ----------

def test_bot_edits_default_to_the_user_and_round_trip():
    assert MoveEdit(page_key="k", to_index=0).by == "user"
    e = TypeAdapter(PathEdit).validate_python({"op": "move", "page_key": "k", "to_index": 1,
                                               "by": "bot"})
    assert e.by == "bot" and TypeAdapter(PathEdit).validate_python(e.model_dump()) == e


def test_a_bot_move_is_shown_but_not_a_user_move():
    items = apply_edits(BASE, [MoveEdit(page_key=D.page_id, to_index=0, by="bot")])
    assert keys(items) == ["path-x-p4", "path-x-p1", "path-x-p2", "path-x-p3"]
    assert not items[0].moved and items[0].bot_moved


def test_a_replan_keeps_the_users_move_and_reorders_the_bots():
    """The user moved C first; the Bot later moved D before B. A replan keeps the user's place
    for C and treats D like any proposal page (here: back at the end)."""
    edits = [MoveEdit(page_key=C.page_id, to_index=0),
             MoveEdit(page_key=D.page_id, to_index=2, by="bot")]
    old = Rec(path(BASE), edits=edits)
    assert keys(apply_edits(BASE, edits)) == ["path-x-p3", "path-x-p1", "path-x-p4", "path-x-p2"]
    m = merge_replan(old, proposal(page(1, ("a",)), page(2, ("b",), ("a",)),
                                   page(3, ("c",), ("b",)), page(4, ("d",))))
    shown = merged_keys(m)
    assert shown[0] == "path-x-p3"                                   # the user's move stays
    assert shown.index("path-x-p4") > shown.index("path-x-p2")       # the Bot's move does not
    assert all(e.by == "user" for e in m.edits)


def test_assistant_edits_count_as_the_users_own():
    """The assistant acts on the user's request: its move stays through a replan, its page is
    the user's own."""
    edits = [MoveEdit(page_key=D.page_id, to_index=0, by="assistant")]
    items = apply_edits(BASE, edits)
    assert items[0].moved and not items[0].bot_moved
    m = merge_replan(Rec(path(BASE), edits=edits), proposal(page(1, ("a",)), page(2, ("b",), ("a",)),
                                                             page(3, ("c",), ("b",)), page(4, ("d",))))
    assert merged_keys(m)[0] == "path-x-p4"
    key = user_key("E", "e", [p.page_id for p in BASE])
    own = apply_edits((*BASE, own_page(key, "e", "E")),
                      [AddEdit(page_key=key, title="E", node_id="e", by="assistant")])
    assert next(i for i in own if i.key == key).mine


def test_a_bot_added_page_is_not_the_users_own_page():
    key = user_key("E", "e", [p.page_id for p in BASE])
    pages = (*BASE, own_page(key, "e", "E"))
    edits = [AddEdit(page_key=key, title="E", node_id="e", by="bot"),
             MoveEdit(page_key=key, to_index=1, by="bot")]
    items = apply_edits(pages, edits)
    assert keys(items)[1] == key
    e = next(i for i in items if i.key == key)
    assert not e.mine and not e.moved and e.page is not None
