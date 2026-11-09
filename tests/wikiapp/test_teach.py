"""R1 Task 2: teaching-spec flows over a Repo (versions, migration on first write, old guideline
API over the spec, exemplars, restore read back from the log)."""

import pytest

from evowiki.teach.contracts import RepParams
from evowiki.teach.default_spec import DEFAULT_ITEMS
from evowiki.wikiapp import teach
from evowiki.wikiapp.events import (
    Guideline,
    GuidelineAdded,
    TeachingSpecSaved,
)
from evowiki.wikiapp.guidelines import add_guideline, list_guidelines, remove_guideline
from evowiki.wikiapp.repo import MemoryRepo, WikiConflict
from evowiki.wikiapp.teach import (
    add_exemplar_flow,
    add_item_flow,
    commit_spec,
    edit_item_flow,
    get_spec,
    list_versions,
    read_version,
    remove_exemplar_flow,
    remove_item_flow,
    restore_flow,
    set_params_flow,
    version_out,
)
from evowiki.wikiapp.teachspec import SpecInvalid, add_item
from evowiki.wikiapp.wiki_schemas import (
    GuidelineIn,
    SpecItemIn,
    SpecItemPatchIn,
    VaultNoteExemplarIn,
    WikiPageExemplarIn,
)
from tests.wikiapp.script import Script
from tests.wikiapp.test_goal import NOW, Ids
from tests.wikiapp.test_organize import page1, run, script_page
from tests.wikiapp.test_page import commit_wiki

OLD = (Guideline(guideline_id="gl_old1", text="多用图", source="user"),
       Guideline(guideline_id="gl_old2", text="先给公式", source="feedback",
                 evidence="你两次要公式"))


@pytest.fixture
def env():
    return MemoryRepo(["u1"]), Script(), Ids()


def st(env):
    return env[0].load("u1").state


def kw(env, **more):
    return {"now": NOW, "new_id": env[2], **more}


def spec_events(env) -> list[TeachingSpecSaved]:
    return [e.body for e in env[0].wiki_log("u1") if isinstance(e.body, TeachingSpecSaved)]


def old_log(env):
    """A pre-branch log: guidelines written as GuidelineAdded events."""
    commit_wiki(env, *[GuidelineAdded(guideline=g) for g in OLD])


def add(env, text="每段先给结论", **more):
    return add_item_flow(env[0], "u1", SpecItemIn(text=text, **more), **kw(env))


# ---- read / migration on first write ----

def test_virtual_v1_then_first_write_is_v1_migrated_and_v2_in_one_commit(env):
    repo = env[0]
    old_log(env)
    v = get_spec(st(env))
    assert (v.version, v.stored) == (1, False)
    assert [i.item_id for i in v.items] == [*(i.item_id for i in DEFAULT_ITEMS), "gl_old1", "gl_old2"]
    assert v.evidence == {}                                  # nothing stored yet
    assert list_versions(st(env)).model_dump() == {"current": 1, "items": []}
    n = len(repo.wiki_log("u1"))

    out = add(env, scope="concept", domain=" 统计 ")
    log = repo.wiki_log("u1")[n:]
    assert [type(e.body).__name__ for e in log] == ["TeachingSpecSaved", "TeachingSpecSaved"]
    v1, v2 = (e.body for e in log)
    assert (v1.spec.version, v1.action, v1.evidence) == (1, "migrated", {"gl_old2": "你两次要公式"})
    assert v1.spec.items == tuple(v.items)                   # v1 = what GET showed
    assert (v2.spec.version, v2.action) == (2, "edit")
    assert (out.version, out.stored) == (2, True)
    new = out.items[-1]
    assert new.item_id.startswith("si_")
    assert (new.text, new.scope, new.domain, new.origin) == ("每段先给结论", "concept", "统计", "user_ui")
    assert out.evidence == {"gl_old2": "你两次要公式"}
    assert [(m.version, m.action) for m in list_versions(st(env)).items] == \
        [(1, "migrated"), (2, "edit")]


def test_write_side_versions_are_previous_plus_one(env):
    repo = env[0]
    add(env)
    sid = st(env).spec.items[-1].item_id
    edit_item_flow(repo, "u1", sid, SpecItemPatchIn(scope="quick"), **kw(env))
    set_params_flow(repo, "u1", RepParams(analogy_level=2), **kw(env))
    add_exemplar_flow(repo, "u1", VaultNoteExemplarIn(kind="vault_note", ref="a.md", title="A",
                                                      text="正文"), **kw(env))
    remove_item_flow(repo, "u1", sid, **kw(env))
    restore_flow(repo, "u1", 2, **kw(env))
    add_guideline(repo, "u1", GuidelineIn(text="少用表格"), **kw(env))
    versions = [b.spec.version for b in spec_events(env)]
    assert versions == list(range(1, len(versions) + 1)) and len(versions) == 8
    assert [b.action for b in spec_events(env)] == [
        "migrated", "edit", "edit", "params", "exemplar", "edit", "restore", "guideline"]
    # a change that does not produce previous + 1 is refused before anything is written
    n = len(repo.wiki_log("u1"))
    with pytest.raises(RuntimeError):
        commit_spec(repo, repo.load("u1"),
                    lambda s: add_item(s, item_id="x", text="t").model_copy(update={"version": 99}),
                    actor="user", action="edit", **kw(env))
    assert len(repo.wiki_log("u1")) == n


def test_base_version_mismatch_is_409_and_writes_nothing(env):
    repo = env[0]
    with pytest.raises(WikiConflict, match="第 1 版"):
        add_item_flow(repo, "u1", SpecItemIn(text="x"), **kw(env, base_version=2))
    assert repo.wiki_log("u1") == []
    assert add_item_flow(repo, "u1", SpecItemIn(text="x"), **kw(env, base_version=1)).version == 2
    with pytest.raises(WikiConflict):
        set_params_flow(repo, "u1", RepParams(), **kw(env, base_version=1))
    assert set_params_flow(repo, "u1", RepParams(), **kw(env, base_version=2)).version == 3


def test_no_item_count_cap_but_field_caps_are_spec_invalid(env):
    repo = env[0]
    for i in range(31 - len(DEFAULT_ITEMS)):
        add(env, text=f"规则 {i}")
    add_guideline(repo, "u1", GuidelineIn(text="第 32 条"), **kw(env))
    assert len(st(env).spec.items) == 32
    n = len(repo.wiki_log("u1"))
    with pytest.raises(SpecInvalid):
        edit_item_flow(repo, "u1", "d01", SpecItemPatchIn.model_construct(domain="领" * 61),
                       **kw(env))
    assert len(repo.wiki_log("u1")) == n


def test_more_than_30_old_guidelines_migrate_whole(env):
    commit_wiki(env, *[GuidelineAdded(guideline=Guideline(guideline_id=f"g{i}", text=f"原则 {i}",
                                                          source="user")) for i in range(31)])
    assert len(get_spec(st(env)).items) == len(DEFAULT_ITEMS) + 31
    add(env)
    assert len(st(env).spec.items) == len(DEFAULT_ITEMS) + 32


# ---- items / params ----

def test_edit_item_fields_and_domain_keep_or_clear(env):
    repo = env[0]
    add(env, domain="生物")
    sid = st(env).spec.items[-1].item_id
    out = edit_item_flow(repo, "u1", sid, SpecItemPatchIn(scope="exercise"), **kw(env))
    assert (out.items[-1].scope, out.items[-1].domain) == ("exercise", "生物")
    out = edit_item_flow(repo, "u1", sid, SpecItemPatchIn(domain=None), **kw(env))
    assert out.items[-1].domain is None
    out = edit_item_flow(repo, "u1", "d01", SpecItemPatchIn(text="  先说人话  "), **kw(env))
    assert (out.items[0].text, out.items[0].authority, out.items[0].item_id) == \
        ("先说人话", "user", "d01")
    with pytest.raises(LookupError):
        edit_item_flow(repo, "u1", "nope", SpecItemPatchIn(scope="any"), **kw(env))
    with pytest.raises(LookupError):
        remove_item_flow(repo, "u1", "nope", **kw(env))
    out = remove_item_flow(repo, "u1", sid, **kw(env))
    assert sid not in {i.item_id for i in out.items}
    out = set_params_flow(repo, "u1", RepParams(recap_max_per_page=0, interactive_bias=2),
                          **kw(env))
    assert (out.params.recap_max_per_page, out.params.interactive_bias) == (0, 2)


# ---- versions / restore ----

def test_restore_an_old_version_read_back_from_the_log(env):
    repo = env[0]
    old_log(env)
    snap = repo.load("u1")
    assert read_version(repo, snap, 1).items == tuple(get_spec(snap.state).items)  # virtual v1
    with pytest.raises(LookupError):
        read_version(repo, snap, 2)
    add(env, text="A")                                        # v1 migrated, v2 +A
    set_params_flow(repo, "u1", RepParams(fading_level=2), **kw(env))       # v3
    remove_item_flow(repo, "u1", "gl_old1", **kw(env))                      # v4
    v2 = version_out(repo, "u1", 2)
    assert v2.version == 2 and [i.text for i in v2.items][-3:] == ["多用图", "先给公式", "A"]
    assert v2.params == RepParams()
    with pytest.raises(LookupError):
        version_out(repo, "u1", 9)
    out = restore_flow(repo, "u1", 2, **kw(env))
    assert out.version == 5
    assert out.items == v2.items and out.params == RepParams()
    m = list_versions(st(env)).items[-1]
    assert (m.version, m.action, m.origin) == (5, "restore", "v2")
    assert "多用图" in st(env).principles()
    with pytest.raises(LookupError):
        restore_flow(repo, "u1", 7, **kw(env))


def test_read_version_uses_the_repo_log_not_state(env):
    repo = env[0]
    add(env, text="A")
    add(env, text="B")
    calls = []
    real = repo.wiki_events_of
    repo.wiki_events_of = lambda uid, types: calls.append(tuple(types)) or real(uid, types)
    assert [i.text for i in version_out(repo, "u1", 2).items][-1] == "A"
    assert calls == [("teaching_spec_saved",)]


# ---- old /wiki/guidelines API over the spec ----

def test_old_api_round_trip_on_a_pre_branch_log(env):
    repo = env[0]
    old_log(env)
    before = list_guidelines(st(env))
    assert [(g.guideline_id, g.source, g.evidence) for g in before.items] == \
        [("gl_old1", "user", None), ("gl_old2", "feedback", "你两次要公式")]
    g = add_guideline(repo, "u1", GuidelineIn(text="  少用表格 "), **kw(env))
    assert (g.text, g.source, g.evidence) == ("少用表格", "user", None)
    after = list_guidelines(st(env))
    assert after.items == [*before.items, g]                 # defaults are not listed
    remove_guideline(repo, "u1", "gl_old1", **kw(env))
    assert [x.guideline_id for x in list_guidelines(st(env)).items] == ["gl_old2", g.guideline_id]
    assert st(env).principles() == ("先给公式", "少用表格")
    with pytest.raises(LookupError):
        remove_guideline(repo, "u1", "gl_old1", **kw(env))
    types = {type(e.body).__name__ for e in repo.wiki_log("u1")[3:]}   # after started + 2 old
    # no GuidelineAdded/Removed written; the delete is recorded as the user's decision
    assert types == {"TeachingSpecSaved", "RuleDecided"}
    assert [b.action for b in spec_events(env)] == ["migrated", "guideline", "guideline"]


# ---- exemplars ----

def test_exemplars_wiki_page_snapshot_vault_note_and_a_4th_rejected(env, monkeypatch):
    repo, s, _ = env
    pid = page1(env)
    with pytest.raises(SpecInvalid, match="还没整理"):
        add_exemplar_flow(repo, "u1", WikiPageExemplarIn(kind="wiki_page", ref=pid), **kw(env))
    with pytest.raises(LookupError):
        add_exemplar_flow(repo, "u1", WikiPageExemplarIn(kind="wiki_page", ref="pg_x"),
                          **kw(env))
    assert spec_events(env) == []
    script_page(s)
    run(env, pid)
    out = add_exemplar_flow(repo, "u1", WikiPageExemplarIn(kind="wiki_page", ref=pid),
                            **kw(env))
    ex = out.exemplars[0]
    assert (ex.kind, ex.ref, ex.title) == ("wiki_page", pid, st(env).pages[pid].title)
    assert ex.text.startswith(f"# {ex.title}\n\n## 从你会的出发")
    assert "## z 分数怎么读" in ex.text and "> s2 的要点" in ex.text and "s1 正文" in ex.text
    assert "## 确认一下" in ex.text and "- 低一个标准差" in ex.text
    vault = VaultNoteExemplarIn(kind="vault_note", ref="notes/a.md", title="A", text="正文")
    add_exemplar_flow(repo, "u1", vault, **kw(env))
    out = add_exemplar_flow(repo, "u1", vault, **kw(env))
    assert [e.kind for e in out.exemplars] == ["wiki_page", "vault_note", "vault_note"]
    n = len(repo.wiki_log("u1"))
    with pytest.raises(SpecInvalid, match="范例最多 3 个"):
        add_exemplar_flow(repo, "u1", vault, **kw(env))
    assert len(repo.wiki_log("u1")) == n
    out = remove_exemplar_flow(repo, "u1", ex.exemplar_id, **kw(env))
    assert [e.kind for e in out.exemplars] == ["vault_note", "vault_note"]
    with pytest.raises(LookupError):
        remove_exemplar_flow(repo, "u1", ex.exemplar_id, **kw(env))
    monkeypatch.setattr(teach, "MAX_EXEMPLAR_TEXT", 20)
    with pytest.raises(SpecInvalid, match="范例最多 20 字"):
        add_exemplar_flow(repo, "u1", WikiPageExemplarIn(kind="wiki_page", ref=pid), **kw(env))
