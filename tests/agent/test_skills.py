"""agent.skills: SKILL.md frontmatter, references with includes, read_ref (no LLM)."""

import pytest

from evowiki.agent.skills import (
    READ_REF_CHARS,
    ReadRefTool,
    SkillError,
    load_skill,
    read_ref,
)
from evowiki.agent.tools import ToolContext

CTX = ToolContext(user_id="u", session_id="s", page_id="p")
FRONT = "---\nname: demo\ndescription: 演示\nrefs:\n  how: 做之前读\n  sub/one: 第一类\n---\n正文。\n{refs}\n"


def _skill(tmp_path, front=FRONT, refs=None):
    root = tmp_path / "skills"
    (root / "demo" / "references" / "sub").mkdir(parents=True)
    (root / "demo" / "SKILL.md").write_text(front, encoding="utf-8")
    for name, text in (refs or {"how": "怎么做\n{{include:rules a b}}", "sub/one": "一类"}).items():
        (root / "demo" / "references" / f"{name}.md").write_text(text, encoding="utf-8")
    return root


def test_load_fills_the_refs_list_and_reads_refs_with_includes(tmp_path):
    s = load_skill("demo", _skill(tmp_path))
    assert s.description == "演示" and list(s.refs) == ["how", "sub/one"]
    assert s.body == "正文。\n- how：做之前读\n- sub/one：第一类"
    assert read_ref(s, "how", {"rules": lambda *a: "规则" + "".join(a)}) == "怎么做\n规则ab"
    assert read_ref(s, "sub/one", appendix=lambda r: "模板" if r == "sub/one" else "") == "一类\n\n模板"


@pytest.mark.parametrize("bad", ["unknown include", "names a file", "listed ref without file",
                                 "no frontmatter"])
def test_what_cannot_be_read_as_written_fails_closed(tmp_path, bad):
    if bad == "unknown include":
        s = load_skill("demo", _skill(tmp_path))
        with pytest.raises(SkillError, match="unknown include"):
            read_ref(s, "how", {})
    elif bad == "names a file":
        s = load_skill("demo", _skill(tmp_path, refs={"how": "见 references/x.md", "sub/one": "x"}))
        with pytest.raises(SkillError, match="another reference"):
            read_ref(s, "how")
    elif bad == "listed ref without file":
        with pytest.raises(SkillError, match="has no file"):
            load_skill("demo", _skill(tmp_path, refs={"how": "x"}))
    else:
        with pytest.raises(SkillError, match="no frontmatter"):
            load_skill("demo", _skill(tmp_path, front="正文"))


def test_read_ref_tool_heads_each_ref_lists_choices_and_splits_long_reads(tmp_path):
    root = _skill(tmp_path, refs={"how": "长" * (READ_REF_CHARS - 5), "sub/one": "一类"})
    tool = ReadRefTool([load_skill("demo", root)])
    got = tool.run({"names": ["sub/one", "demo/sub/one"]}, CTX)
    assert got.error is None and got.content.startswith("【sub/one】\n一类")
    assert tool.run({"names": ["nope"]}, CTX).error is not None
    assert "how、sub/one" in tool.run({"names": ["nope"]}, CTX).error
    both = tool.run({"names": ["how", "sub/one"]}, CTX)
    assert len(both.content) <= 11000 and "这些下次再读：sub/one" in both.content
