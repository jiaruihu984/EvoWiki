"""agent.skills (stage 3 T2): the Skill files, their load-time checks, versions, the two tools and
`activate`. No model, no database."""

from pathlib import Path

import pytest

from evowiki.agent.catalog import (
    Activation,
    LoadSkillTool,
    ReadRefTool,
    SkillError,
    activate,
    load_library,
)
from evowiki.agent.tools import ToolContext

CTX = ToolContext(user_id="u1", session_id="s1")


def _skill(root: Path, key: str, meta: str = "", body: str = "规则一", refs: dict | None = None,
           order: int = 10) -> Path:
    d = root / key
    (d / "references").mkdir(parents=True, exist_ok=True)
    lines = [f"key: {key}", f"name: 名字{key}", f"when: 什么时候用 {key}", f"order: {order}"]
    if refs:
        lines.append("refs:")
        for name, (when, tools, text) in refs.items():
            tl = f", tools: [{', '.join(tools)}]" if tools else ""
            lines.append(f"  {name}: {{when: {when}{tl}}}")
            (d / "references" / f"{name}.md").write_text(text, encoding="utf-8", newline="\n")
    (d / "SKILL.md").write_text("---\n" + "\n".join(lines) + ("\n" + meta if meta else "")
                                + "\n---\n" + body + "\n", encoding="utf-8", newline="\n")
    return d


def test_frontmatter_rules_refs_and_catalog(tmp_path):
    _skill(tmp_path, "b-two", "tools: [t2, t1]", order=20)
    _skill(tmp_path, "a-one", "tools: [t1]", refs={"graph": ("改图谱时", ["t3"], "先查 id")})
    lib = load_library(tmp_path, registry={"t1", "t2", "t3"})
    assert lib.keys() == ("a-one", "b-two")                         # by order
    a = lib.get("a-one")
    assert a is not None and a.rules == "规则一" and a.tools == ("t1",)
    assert lib.ref("a-one/graph").tools == ("t3",) and lib.ref("a-one/graph").body == "先查 id"
    assert lib.catalog().splitlines()[0] == "- a-one（名字a-one）：什么时候用 a-one。工具：t1。参考：a-one/graph（t3）"


def test_version_is_a_stable_content_hash(tmp_path):
    d = _skill(tmp_path, "k", refs={"r": ("w", [], "正文")})
    v1 = load_library(tmp_path).get("k").version
    assert v1 == load_library(tmp_path).get("k").version and len(v1) == 8
    (d / "SKILL.md").write_bytes((d / "SKILL.md").read_bytes().replace(b"\n", b"\r\n"))
    assert load_library(tmp_path).get("k").version == v1           # line ends do not count
    (d / "references" / "r.md").write_text("正文改了", encoding="utf-8", newline="\n")
    lib = load_library(tmp_path)
    assert lib.get("k").version != v1 and lib.versions({"k"}, {"k/r"})["k/r"] == lib.ref("k/r").version


@pytest.mark.parametrize("meta", ["tier: direct", "permissions: [x]", "grant: all"])
def test_a_skill_never_carries_a_tier_or_permission(tmp_path, meta):
    _skill(tmp_path, "k", meta)
    with pytest.raises(SkillError, match="unknown frontmatter"):
        load_library(tmp_path)


def test_a_reference_naming_another_reference_is_refused(tmp_path):
    _skill(tmp_path, "k", refs={"a": ("w", [], "先读 k/b 再说"), "b": ("w", [], "b 的正文")})
    with pytest.raises(SkillError, match="names another reference"):
        load_library(tmp_path)


def test_nested_or_undeclared_references_are_refused(tmp_path):
    d = _skill(tmp_path, "k", refs={"a": ("w", [], "a")})
    (d / "references" / "extra.md").write_text("x", encoding="utf-8", newline="\n")
    with pytest.raises(SkillError, match="not declared"):
        load_library(tmp_path)


def test_an_unknown_tool_is_a_load_error(tmp_path):
    _skill(tmp_path, "k", "tools: [nope]")
    with pytest.raises(SkillError, match="not in the registry"):
        load_library(tmp_path, registry={"t1"})


def test_load_skill_and_read_ref_say_what_they_loaded_and_dedupe(tmp_path):
    _skill(tmp_path, "k", "tools: [t1]", refs={"g": ("改图谱时", ["t2"], "图谱规则")})
    act = Activation(load_library(tmp_path))
    load, read = LoadSkillTool(lambda: act), ReadRefTool(lambda: act)
    r = load.run({"skills": ["k"]}, CTX)
    assert r.error is None and r.loaded == ("k",) and "规则一" in r.content and "k/g" in r.content
    act.note(r)
    assert act.skills() == ("k",)
    r2 = read.run({"path": "k/g"}, CTX)
    assert r2.loaded == ("k/g",) and "图谱规则" in r2.content
    act.note(r2)
    again = read.run({"path": "k/g"}, CTX)
    assert "读过" in again.content and "图谱规则" not in again.content
    assert "已经载入" in load.run({"skills": ["k"]}, CTX).content


def test_an_unknown_key_or_path_is_a_tool_error_listing_the_valid_ones(tmp_path):
    _skill(tmp_path, "k", refs={"g": ("w", [], "x")})
    act = Activation(load_library(tmp_path))
    bad = LoadSkillTool(lambda: act).run({"skills": ["k", "nope"]}, CTX)
    assert bad.error is not None and "nope" in bad.content and "k" in bad.content
    assert bad.loaded == ()                                   # nothing half-loaded
    act.note(bad)
    assert act.skills() == ()
    ref = ReadRefTool(lambda: act).run({"path": "k/zz"}, CTX)
    assert ref.error is not None and "k/g" in ref.content


def test_activate_orders_core_then_catalog_and_keeps_only_built_tools(tmp_path):
    _skill(tmp_path, "a", "tools: [t1, t2]", refs={"r": ("w", ["t3"], "x")}, order=1)
    _skill(tmp_path, "b", "tools: [t2, t4]", order=2)
    lib = load_library(tmp_path)
    ts = activate(lib, available=["t4", "t3", "t2", "t1", "c1", "undo"], core=["c1", "c9"],
                  skills={"b", "a"}, refs={"a/r"}, also=["undo"])
    assert ts.names == ("c1", "t1", "t2", "t3", "t4", "undo")
    assert set(ts.versions) == {"a", "b", "a/r"}
    assert activate(lib, available=["t1", "c1"], core=["c1"], skills=set(), refs=set()).names \
        == ("c1",)


def test_the_assistant_library_loads_and_every_assistant_tool_has_a_home():
    from evowiki.assistant.skills import CORE, UNDO, library
    from evowiki.harness.tool_names import TOOLS
    lib = library()
    homes = {*CORE, *lib.all_tools(), UNDO}
    assert set(TOOLS["assistant"]) <= homes
    assert lib.keys() == ("remember", "plan-goal", "assess", "edit-wiki", "interact", "sources",
                          "settings", "conflicts")


def test_load_skill_after_read_ref_still_gives_the_rules(tmp_path):
    _skill(tmp_path, "k", body="技能规则", refs={"g": ("w", ["t2"], "参考正文")})
    act = Activation(load_library(tmp_path))
    act.note(ReadRefTool(lambda: act).run({"path": "k/g"}, CTX))
    assert act.skills() == ("k",) and "k" not in act.ruled()
    r = LoadSkillTool(lambda: act).run({"skills": ["k"]}, CTX)
    assert "技能规则" in r.content                          # a read_ref alone gave no rules
    act.note(r)
    assert "已经载入" in LoadSkillTool(lambda: act).run({"skills": ["k"]}, CTX).content


def test_a_carried_reference_is_read_again_in_full():
    from evowiki.assistant.skills import library
    act = Activation(library(), carried_refs=("edit-wiki/path",))
    r = ReadRefTool(lambda: act).run({"path": "edit-wiki/path"}, CTX)
    assert "covers" in r.content                            # the body, not "already read"


def test_a_session_with_attachments_preloads_plan_goal_too():
    from evowiki.assistant.skills import preload
    p = preload(learn_goal=False, session_attachments=True, message_attachments=False,
                wrong_answer=False, open_blocks=False)
    assert p.skills == ("plan-goal", "sources")
