import pytest

from evowiki.teach import spec_ops
from evowiki.teach.contracts import SUGGESTED_SPEC_ITEMS, ParamPatchOp, SpecItem, SpecPatchOp
from evowiki.teach.spec_ops import PatchError
from tests.teach.spec_helpers import ITEMS, spec


def _full():
    return spec(*(SpecItem(item_id=f"i{n}", text=f"规则 {n}", origin="default", authority="rsi")
                  for n in range(SUGGESTED_SPEC_ITEMS)))


def test_add_edit_remove_and_param():
    s = spec()
    added = spec_ops.apply(s, SpecPatchOp(op="add_item", item_id="rsi-p1", text="先讲后用"),
                           patch_id="p1")
    assert added.items[-1].item_id == "rsi-p1" and added.items[-1].authority == "rsi"
    assert (added.items[-1].origin, added.items[-1].origin_ref) == ("rsi_h", "p1")
    assert added.complexity() == s.complexity() + 1
    edited = spec_ops.apply(s, SpecPatchOp(op="edit_item", item_id="s2", text="术语先讲"),
                            patch_id="p2")
    assert [i.text for i in edited.items if i.item_id == "s2"] == ["术语先讲"]
    assert edited.complexity() == s.complexity()
    removed = spec_ops.apply(s, SpecPatchOp(op="remove_item", item_id="s2"), patch_id="p3")
    assert "s2" not in {i.item_id for i in removed.items}
    moved = spec_ops.apply(s, ParamPatchOp(track="B", path="analogy_level", value=0),
                           patch_id="p4")
    assert moved.params.analogy_level == 0 and moved.complexity() == s.complexity() + 1


@pytest.mark.parametrize("body", [
    SpecPatchOp(op="edit_item", item_id="nope", text="x"),
    SpecPatchOp(op="add_item", item_id="s1", text="x"),            # id in use
    SpecPatchOp(op="add_item", text="x"),                          # no id
    SpecPatchOp(op="edit_item", item_id="s1", text=ITEMS[0].text),  # no change
    ParamPatchOp(track="B", path="analogy_level", value=3),        # out of range
    ParamPatchOp(track="B", path="analogy_level", value=1.5),
    ParamPatchOp(track="B", path="analogy_level", value=1),        # already 1
    ParamPatchOp(track="B", path="slip", value=0.1),               # not a RepParams path
    ParamPatchOp(track="A", path="analogy_level", value=0),
])
def test_invalid_bodies_are_refused(body):
    with pytest.raises(PatchError):
        spec_ops.apply(spec(), body, patch_id="p")


def test_no_item_count_cap():
    out = spec_ops.apply(_full(), SpecPatchOp(op="add_item", item_id="x", text="再一条"), patch_id="p")
    assert len(out.items) == SUGGESTED_SPEC_ITEMS + 1
    spec_ops.apply(_full(), SpecPatchOp(op="edit_item", item_id="i0", text="改"), patch_id="p")


def test_revert_restores_what_the_patch_touched_and_keeps_later_changes():
    s = spec()
    body = SpecPatchOp(op="edit_item", item_id="s2", text="术语先讲")
    after = spec_ops.apply(s, body, patch_id="p1")
    later = spec_ops.apply(after, SpecPatchOp(op="add_item", item_id="z", text="另一条"),
                           patch_id="p2")
    back = spec_ops.revert(later, s, body)
    assert [i.text for i in back.items if i.item_id == "s2"] == [ITEMS[1].text]
    assert "z" in {i.item_id for i in back.items}
    rm = SpecPatchOp(op="remove_item", item_id="s2")
    assert spec_ops.revert(spec_ops.apply(s, rm, patch_id="p"), s, rm).items == s.items
    add = SpecPatchOp(op="add_item", item_id="n", text="新")
    assert spec_ops.revert(spec_ops.apply(s, add, patch_id="p"), s, add).items == s.items
    par = ParamPatchOp(track="B", path="fading_level", value=0)
    assert spec_ops.revert(spec_ops.apply(s, par, patch_id="p"), s, par).params == s.params
    with pytest.raises(PatchError):
        spec_ops.revert(s, s, add)                                 # nothing to undo


def test_body_key_ignores_spacing_punctuation_and_fresh_add_ids():
    a = SpecPatchOp(op="add_item", item_id="rsi-1", text="术语先讲，再用。")
    b = SpecPatchOp(op="add_item", item_id="rsi-2", text=" 术语先讲 再用")
    assert spec_ops.body_key(a) == spec_ops.body_key(b)
    c = SpecPatchOp(op="add_item", item_id="rsi-3", text="术语后讲")
    assert spec_ops.body_key(a) != spec_ops.body_key(c)


def test_describe_is_plain_chinese():
    s = spec()
    assert spec_ops.describe(ParamPatchOp(track="B", path="analogy_level", value=0), s) == \
        "把「类比允许程度」调低了一些"
    assert spec_ops.describe(SpecPatchOp(op="remove_item", item_id="s2"), s).startswith(
        "去掉了「用简练")


def test_describe_for_model_keeps_the_informative_format():
    s = spec()
    assert spec_ops.describe_for_model(
        ParamPatchOp(track="B", path="analogy_level", value=0), s) == "调整参数「类比允许程度」：1 → 0"
    assert spec_ops.describe_for_model(SpecPatchOp(op="remove_item", item_id="s2"), s).startswith(
        "删除教学规范：「用简练")
    assert spec_ops.describe(ParamPatchOp(track="B", path="analogy_level", value=1), s) == \
        "把「类比允许程度」调整了"
