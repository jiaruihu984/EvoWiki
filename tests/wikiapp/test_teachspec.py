from datetime import UTC, datetime

import pytest

from evowiki.teach.contracts import Exemplar, RepParams, SpecItem, TeachingSpec
from evowiki.teach.default_spec import DEFAULT_ITEMS, default_spec
from evowiki.teach.render import guideline_lines, param_lines
from evowiki.wikiapp.events import Guideline, PageRecord
from evowiki.wikiapp.state import WikiState
from evowiki.wikiapp.teachspec import (
    KEEP,
    SpecInvalid,
    add_exemplar,
    add_item,
    append_imported,
    applicable,
    edit_item,
    effective_spec,
    effective_version,
    migrate,
    migration_evidence,
    page_kind,
    remove_exemplar,
    remove_item,
    restore,
    set_params,
    spec_domains,
)

T0 = datetime(2026, 10, 9, 9, 0, tzinfo=UTC)


def gl(gid, text, source="user", evidence=None):
    return Guideline(guideline_id=gid, text=text, source=source, evidence=evidence)


def item(iid, scope="any", domain=None, origin="user_ui", text=None):
    return SpecItem(item_id=iid, text=text or f"规则 {iid}", scope=scope, domain=domain,
                    origin=origin)


def exemplar(eid):
    return Exemplar(exemplar_id=eid, kind="vault_note", ref=f"n/{eid}.md", title=eid, text="正文")


# ---- default spec ----

def test_default_spec_valid():
    spec = default_spec()
    assert isinstance(spec, TeachingSpec) and spec.version == 1
    # 22 since the teaching standard v2 (2026-10-10): the standard's rules as editable items, leaving
    # the user 8 of the 30 slots
    assert 1 <= len(DEFAULT_ITEMS) <= 22
    ids = [i.item_id for i in DEFAULT_ITEMS]
    assert ids == [f"d{n:02d}" for n in range(1, len(ids) + 1)] and len(set(ids)) == len(ids)
    assert all((i.origin, i.authority) == ("default", "rsi") and i.domain is None for i in DEFAULT_ITEMS)
    assert {i.scope for i in DEFAULT_ITEMS} == {"any", "concept", "exercise", "quick"}
    # 面试 left the list: 「可迁移规则与面试表达」 is part of the standard, not private vault detail
    private = ("Zotero", "Ingestion", "Jerry", "Obsidian", "Python", "Ctrl")
    assert not any(p in i.text for i in DEFAULT_ITEMS for p in private)


# ---- migration ----

def test_migrate_nothing_is_defaults():
    assert migrate(()) == default_spec()
    assert effective_spec(WikiState()) == default_spec()
    assert effective_version(WikiState()) == 1


def test_migrate_guidelines_after_defaults_ids_kept():
    gls = (gl("gl1", "多举例"), gl("gl2", "先给公式", "feedback", "你两次要公式"))
    spec = migrate(gls)
    assert spec.version == 1 and spec.items[:len(DEFAULT_ITEMS)] == DEFAULT_ITEMS
    u, r = spec.items[len(DEFAULT_ITEMS):]
    assert (u.item_id, u.text, u.origin, u.authority, u.scope, u.domain) == \
        ("gl1", "多举例", "user_ui", "user", "any", None)
    # an old feedback guideline: origin unknown, protected as the user's (rule provenance contract)
    assert (r.item_id, r.text, r.origin, r.authority, r.origin_ref) == \
        ("gl2", "先给公式", "legacy_unknown", "user", "guideline:gl2")
    assert migration_evidence(gls) == {"gl2": "你两次要公式"}
    assert effective_spec(WikiState(guidelines=gls)) == spec


def test_migrate_keeps_every_default_and_guideline():
    gls = tuple(gl(f"gl{i}", f"原则{i}") for i in range(31))
    spec = migrate(gls)
    assert spec.items[:len(DEFAULT_ITEMS)] == DEFAULT_ITEMS
    assert [i.item_id for i in spec.items[len(DEFAULT_ITEMS):]] == [g.guideline_id for g in gls]


def test_effective_spec_prefers_stored():
    stored = TeachingSpec(version=4, items=(item("a"),))
    s = WikiState(spec=stored, guidelines=(gl("gl1", "多举例"),))
    assert effective_spec(s) is stored and effective_version(s) == 4


# ---- applicable ----

def test_page_kind_quick_when_shelved():
    def rec(pid, **kw):
        return PageRecord(page_id=pid, title=pid, goal=pid, concepts=(), targets=(),
                          created_at=datetime(2026, 10, 7, tzinfo=UTC), **kw)
    s = WikiState(pages={"p1": rec("p1"), "p2": rec("p2", shelved=True)}, page_order=("p1", "p2"))
    assert page_kind(s, "p2") == "quick" and page_kind(s, "p1") == "concept"


def test_applicable_by_scope_and_domain():
    spec = TeachingSpec(version=1, items=(
        item("a", "any"), item("c", "concept"), item("e", "exercise"), item("q", "quick"),
        item("bio", "any", " Biology "), item("bioq", "quick", "biology"), item("cs", "concept", "CS")))
    ids = lambda xs: [i.item_id for i in xs]
    # a page without a domain gets the domain items too (the writer sees their domain and judges)
    assert ids(applicable(spec, "concept", None)) == ["a", "c", "e", "bio", "cs"]
    assert ids(applicable(spec, "quick", None)) == ["a", "q", "bio", "bioq"]
    assert ids(applicable(spec, "concept", "biology")) == ["a", "c", "e", "bio"]
    assert ids(applicable(spec, "quick", "BIOLOGY ")) == ["a", "q", "bio", "bioq"]
    assert ids(applicable(spec, "concept", "cs")) == ["a", "c", "e", "cs"]
    assert ids(applicable(spec, "concept", "  ")) == ["a", "c", "e", "bio", "cs"]
    assert ids(applicable(spec, "concept", "history")) == ["a", "c", "e"]   # another domain: out
    assert spec_domains(spec) == ("Biology", "CS")
    assert spec_domains(default_spec()) == ()


# ---- edits ----

def test_edits_increment_version_and_leave_input_untouched():
    s1 = default_spec()
    s2 = add_item(s1, item_id="u1", text="  多举例  ", scope="concept", domain=" 生物 ")
    assert s2.version == 2 and s1.version == 1 and len(s1.items) == len(DEFAULT_ITEMS)
    new = s2.items[-1]
    assert (new.text, new.scope, new.domain, new.origin) == ("多举例", "concept", "生物", "user_ui")
    s3 = edit_item(s2, "d01", text="改过的")
    d01 = s3.items[0]
    # the edit keeps provenance; who owns it afterwards is the checkpoint's (rules.authority)
    assert s3.version == 3 and (d01.item_id, d01.text, d01.origin, d01.scope) == \
        ("d01", "改过的", "default", "any")
    s4 = edit_item(s3, "u1", scope="any")
    assert s4.items[-1].domain == "生物" and s4.items[-1].scope == "any"   # domain kept
    s5 = edit_item(s4, "u1", domain=None)
    assert s5.items[-1].domain is None and s5.items[-1].text == "多举例"
    s6 = remove_item(s5, "d02")
    assert s6.version == 6 and "d02" not in {i.item_id for i in s6.items}
    s7 = set_params(s6, RepParams(analogy_level=2))
    assert s7.version == 7 and s7.params.analogy_level == 2 and s7.items == s6.items
    s8 = add_exemplar(s7, exemplar("x1"))
    s9 = remove_exemplar(s8, "x1")
    assert (s8.version, len(s8.exemplars), s9.version, s9.exemplars) == (8, 1, 9, ())
    assert edit_item(s9, "u1", domain=KEEP).items[-1].domain is None


def test_edit_keeps_counts_and_origin():
    spec = TeachingSpec(version=2, items=(SpecItem(item_id="r", text="t", origin="rsi_b", authority="rsi",
                                                   origin_ref="guideline:r", helpful=2.5, harmful=1.0),))
    r = edit_item(spec, "r", text="新").items[0]
    assert (r.origin, r.origin_ref, r.helpful, r.harmful) == ("rsi_b", "guideline:r", 2.5, 1.0)


def test_unknown_ids_are_lookup_errors():
    spec = default_spec()
    for f in (lambda: edit_item(spec, "nope", text="x"), lambda: remove_item(spec, "nope"),
              lambda: remove_exemplar(spec, "nope")):
        with pytest.raises(LookupError):
            f()


def test_caps_raise_spec_invalid():
    full = TeachingSpec(version=1, items=tuple(item(f"i{n}") for n in range(30)))
    assert len(add_item(full, item_id="x", text="再加一条").items) == 31   # no count cap
    spec = default_spec()
    for bad in ({"text": "   "}, {"text": "长" * 501}, {"text": "ok", "domain": "长" * 61}):
        with pytest.raises(SpecInvalid):
            add_item(spec, item_id="x", **bad)
    with pytest.raises(SpecInvalid):
        add_item(spec, item_id="d01", text="重复的 id")
    with pytest.raises(SpecInvalid):
        edit_item(spec, "d01", text="")
    three = spec
    for n in range(3):
        three = add_exemplar(three, exemplar(f"x{n}"))
    with pytest.raises(SpecInvalid):
        add_exemplar(three, exemplar("x3"))
    with pytest.raises(SpecInvalid):
        add_exemplar(add_exemplar(spec, exemplar("x0")), exemplar("x0"))     # repeated id
    assert issubclass(SpecInvalid, ValueError)


def test_restore_is_a_new_version_with_old_content():
    v1 = default_spec()
    v2 = set_params(add_exemplar(v1, exemplar("x")), RepParams(fading_level=0))
    v3 = remove_item(v2, "d01")
    back = restore(v3, v1)
    assert v3.version == 4 and back.version == 5 and back.items == v1.items and back.exemplars == v1.exemplars
    assert back.params == v1.params


def test_append_imported_appends_whole():
    spec = default_spec()
    imp = tuple(SpecItem(item_id=f"im{n}", text=f"种子{n}", origin="seed", origin_ref="我的规范")
                for n in range(31))
    out = append_imported(spec, imp)
    assert out.version == 2 and out.items[-31:] == imp and len(out.items) == len(spec.items) + 31


# ---- render ----

def test_param_lines_per_value():
    assert param_lines(RepParams()) == (
        "每页提醒卡（已会内容的一句提醒）最多 4 张", "类比只借用他已经确定会的东西",
        "例题先写全一遍，再逐步留空让他自己补", "确实需要时才用交互")
    assert param_lines(RepParams(recap_max_per_page=0, analogy_level=0, fading_level=0,
                                 interactive_bias=0)) == (
        "这一页不放提醒卡", "不用类比", "例题每一步都写全", "尽量不用交互，用文字和静态例子")
    assert param_lines(RepParams(recap_max_per_page=8, analogy_level=2, fading_level=2,
                                 interactive_bias=2)) == (
        "每页提醒卡（已会内容的一句提醒）最多 8 张", "可以多用类比，但要说明类比在哪里不成立",
        "例题尽早留空让他自己做", "能用交互演示的优先用交互")


def test_guideline_lines_items_then_params():
    items = (item("a", text="甲"), item("b", text="乙"))
    lines = guideline_lines(items, RepParams())
    assert lines[:2] == ("甲", "乙") and lines[2:] == param_lines(RepParams())
    assert guideline_lines((), RepParams()) == param_lines(RepParams())
