"""Path edits 2 (assistant-actions task 2): `add` at an index, `replace` (拆成多页) and its undo
(`unreplace`), on planned rows and on rows of the user's own alike, and replanning over them."""

import pytest
from pydantic import TypeAdapter

from evowiki.goals.path_edit import (
    AddEdit,
    MoveEdit,
    PathEdit,
    RemoveEdit,
    ReplaceEdit,
    ReplaceItem,
    UnreplaceEdit,
    apply_edits,
    merge_replan,
    visible,
)
from tests.goals.test_path_edit import BASE, PID, Rec, keys, page, path

P1, P2, P3, P4 = (f"{PID}-p{i}" for i in (1, 2, 3, 4))


def items(*pairs):
    return tuple(ReplaceItem(page_key=k, title=t, node_id=n) for k, t, n in pairs)


def replace(key, *pairs):
    return ReplaceEdit(page_key=key, items=items(*pairs))


KIDS = (("u-k1", "第一小点", "x1"), ("u-k2", "第二小点", "x2"))


def test_add_at_index_puts_the_page_there_and_none_means_the_end():
    got = apply_edits(BASE, [AddEdit(page_key="u-1", title="新", to_index=1)])
    assert keys(got) == [P1, "u-1", P2, P3, P4]
    assert keys(apply_edits(BASE, [AddEdit(page_key="u-1", title="新")])) == [P1, P2, P3, P4, "u-1"]
    assert keys(apply_edits(BASE, [AddEdit(page_key="u-1", title="新", to_index=0)]))[0] == "u-1"
    assert keys(apply_edits(BASE, [AddEdit(page_key="u-1", title="新", to_index=99)]))[-1] == "u-1"


def test_add_at_index_counts_shown_rows_only():
    edits = [RemoveEdit(page_key=P1), AddEdit(page_key="u-1", title="新", to_index=1)]
    assert keys(apply_edits(BASE, edits)) == [P2, "u-1", P3, P4]


def test_replace_swaps_the_row_for_its_items_in_order_and_keeps_the_neighbours():
    got = apply_edits(BASE, [replace(P2, *KIDS)])
    assert keys(got) == [P1, "u-k1", "u-k2", P3, P4]
    row = {i.key: i for i in got}
    assert row[P2].removed and row["u-k1"].mine and row["u-k1"].title == "第一小点"
    assert row["u-k2"].node_ids == ("x2",)


def test_replace_follows_a_user_move_of_the_row_and_later_moves_work_on_the_items():
    moved = [MoveEdit(page_key=P2, to_index=3), replace(P2, *KIDS)]
    assert keys(apply_edits(BASE, moved)) == [P1, P3, P4, "u-k1", "u-k2"]
    later = [*moved, MoveEdit(page_key="u-k2", to_index=0)]
    assert keys(apply_edits(BASE, later)) == ["u-k2", P1, P3, P4, "u-k1"]


@pytest.mark.parametrize("make", ["add", "grow", "split"])
def test_edits_behave_the_same_on_rows_of_the_users_own(make):
    if make == "add":
        first = AddEdit(page_key="u-own", title="自己加的", node_id="n9")
    elif make == "grow":
        first = AddEdit(page_key="u-own", title="看不懂长出来的", node_id="n9", to_index=1)
    else:
        first = replace(P3, ("u-own", "拆出来的", "n9"))
    base = [first]
    shown = keys(apply_edits(BASE, base))
    assert "u-own" in shown
    got = apply_edits(BASE, [*base, MoveEdit(page_key="u-own", to_index=0)])
    assert keys(got)[0] == "u-own"
    got = apply_edits(BASE, [*base, replace("u-own", ("u-a", "甲", "a1"), ("u-b", "乙", "b1"))])
    k = keys(got)
    assert "u-own" not in k and k.index("u-a") + 1 == k.index("u-b")
    got = apply_edits(BASE, [*base, RemoveEdit(page_key="u-own")])
    assert "u-own" not in keys(got)


def test_unreplace_brings_the_row_back_and_hides_the_items():
    edits = [replace(P2, *KIDS), UnreplaceEdit(page_key=P2)]
    got = apply_edits(BASE, edits)
    assert keys(got) == [P1, P2, P3, P4]
    assert {i.key for i in got if i.removed} == {"u-k1", "u-k2"}
    again = apply_edits(BASE, [*edits, replace(P2, ("u-k3", "第三小点", "x3"))])
    assert keys(again) == [P1, "u-k3", P3, P4]


def test_replace_of_a_missing_row_is_ignored_on_replay():
    assert keys(apply_edits(BASE, [replace("nope", *KIDS)])) == [P1, P2, P3, P4]


def test_the_new_edits_round_trip_through_the_union():
    ta = TypeAdapter(list[PathEdit])
    edits = [AddEdit(page_key="u-1", title="a", to_index=2), replace(P1, *KIDS),
             UnreplaceEdit(page_key=P1)]
    assert ta.validate_json(ta.dump_json(edits)) == edits


def test_replan_keeps_the_items_of_a_split_row_where_the_user_had_them():
    p = path(BASE)
    old = Rec(p, edits=[replace(P2, *KIDS)])
    proposal = path((page(1, ("a",)), page(2, ("b",), ("a",)), page(3, ("c",), ("b",)),
                     page(4, ("d",)), page(5, ("e",))))
    merged = merge_replan(old, proposal)
    order = [k for k in merged.order if k not in {i.key for i in apply_edits(
        merged.path.pages, merged.edits) if i.removed}]
    assert order[:3] == [P1, "u-k1", "u-k2"]
    assert order.index(P3) > order.index("u-k2") and P2 not in order
    replay = visible(apply_edits(merged.path.pages, merged.edits))
    assert [i.key for i in replay] == order


def test_unremove_shows_the_row_again_exactly_where_it_was():
    from evowiki.goals.path_edit import UnremoveEdit
    edits = [MoveEdit(page_key=P3, to_index=0), RemoveEdit(page_key=P1)]
    assert keys(visible(apply_edits(BASE, edits))) == [P3, P2, P4]
    got = apply_edits(BASE, [*edits, UnremoveEdit(page_key=P1)])
    assert not any(i.removed for i in got)
    assert keys(visible(got)) == [P3, P1, P2, P4]
    assert TypeAdapter(PathEdit).validate_python({"op": "unremove", "page_key": P1}) == UnremoveEdit(page_key=P1)
