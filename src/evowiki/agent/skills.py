"""Skills: a task's entry text (SKILL.md) and its references, read on demand (design
the product specification A4; first user: the page writer).

Files: `src/evowiki/skills/<name>/SKILL.md` and `src/evowiki/skills/<name>/references/<ref>.md`
(a ref may sit one folder down, e.g. `evoui/chart`). SKILL.md starts with a frontmatter block:

    ---
    name: write-page
    description: one line
    refs:
      outline: when to read it
      evoui/chart: when to read it
    ---

Only these three keys, `refs` one level deep (each value a plain line), so no YAML library is
needed. The body may hold `{refs}`, replaced by the refs list (`- <name>：<when>`).

A reference may pull text whose single source is code with `{{include:<name> <args…>}}` on a line
of its own: the caller's `Includes` maps the name to a function of the args (the teaching
standard's sections, the item-writing rules, …), so a rule is written once. An `Appendix` adds
text after a ref (a ref's EvoUI templates, the pack's H8 templates). An unknown include, a ref
that names another ref file, or a missing file fails when the skill is loaded or read — never a
silently empty prompt.

`ReadRefTool` (`read_ref`) is the model's way in: `names` are refs of the skills it was given
(`write-page/evoui/chart`, or `evoui/chart` when it has one skill). Its results are rules, not
material: they are not elided (keep the tool out of an `ElidePolicy.read_tools`)."""

import hashlib
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from evowiki.agent.tool_helpers import fail, run_guarded, spec
from evowiki.agent.tools import ToolContext, ToolResult

ROOT = Path(__file__).resolve().parent.parent / "skills"
REF_DIR = "references"
_FRONT = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
_INCLUDE = re.compile(r"\{\{include:([a-z_]+)((?: [^{}\s]+)*)\}\}")
_REF_PATH = re.compile(r"references/|\b[a-z-]+\.md\b")
READ_REF_MAX = 4            # refs one call asks for
READ_REF_CHARS = 10_500     # what one result holds (run_guarded clips at HARD_CAP 11000)

Includes = Mapping[str, Callable[..., str]]
Appendix = Callable[[str], str]          # ref name -> text added after it ("" = none)


class SkillError(RuntimeError):
    """A skill or reference that cannot be read as written (fail closed)."""


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    body: str                                  # SKILL.md without the frontmatter, {refs} filled
    refs: Mapping[str, str]                    # ref name -> when to read it
    root: Path = field(default=ROOT)
    version: str = ""                          # "<name>@<sha256[:12] of SKILL.md and every ref file>"

    def ref_path(self, ref: str) -> Path:
        return self.root / self.name / REF_DIR / f"{ref}.md"


def _frontmatter(text: str, where: str) -> tuple[dict[str, Any], str]:
    m = _FRONT.match(text.replace("\r\n", "\n"))
    if m is None:
        raise SkillError(f"{where}: no frontmatter")
    out: dict[str, Any] = {}
    refs: dict[str, str] | None = None
    for line in m.group(1).splitlines():
        if not line.strip():
            continue
        if line.startswith("  ") and refs is not None:
            key, _, val = line.strip().partition(":")
            refs[key.strip()] = val.strip()
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        if key not in ("name", "description", "refs"):
            raise SkillError(f"{where}: unknown frontmatter key {key!r}")
        if key == "refs":
            refs = out.setdefault("refs", {})
        else:
            out[key] = val.strip()
            refs = None
    return out, text.replace("\r\n", "\n")[m.end():]


def load_skill(name: str, root: Path = ROOT) -> Skill:
    """The skill as written; a missing file, a bad frontmatter or a listed ref without its file
    raises SkillError."""
    path = root / name / "SKILL.md"
    if not path.is_file():
        raise SkillError(f"no skill {name!r}")
    meta, body = _frontmatter(path.read_text(encoding="utf-8"), str(path))
    if meta.get("name") != name:
        raise SkillError(f"{path}: name is {meta.get('name')!r}, not {name!r}")
    refs = dict(meta.get("refs") or {})
    skill = Skill(name=name, description=str(meta.get("description", "")), body="", refs=refs,
                  root=root)
    for r in refs:
        if not skill.ref_path(r).is_file():
            raise SkillError(f"{name}: ref {r!r} has no file")
    listing = "\n".join(f"- {r}：{w}" for r, w in refs.items())
    digest = hashlib.sha256(path.read_bytes())
    for r in sorted(refs):
        digest.update(skill.ref_path(r).read_bytes())
    return Skill(name=name, description=skill.description,
                 body=body.replace("{refs}", listing).strip(), refs=refs, root=root,
                 version=f"{name}@{digest.hexdigest()[:12]}")


def read_ref(skill: Skill, ref: str, includes: Includes = {},  # noqa: B006 - read only
             appendix: Appendix | None = None) -> str:
    """One reference with its includes expanded and its appendix added. An unknown ref or
    include, or a ref naming another ref file, raises SkillError."""
    if ref not in skill.refs:
        raise SkillError(f"{skill.name}: no ref {ref!r}")
    text = skill.ref_path(ref).read_text(encoding="utf-8").replace("\r\n", "\n")
    if _REF_PATH.search(text):
        raise SkillError(f"{skill.name}/{ref}: a reference names another reference file")

    def expand(m: re.Match[str]) -> str:
        fn = includes.get(m.group(1))
        if fn is None:
            raise SkillError(f"{skill.name}/{ref}: unknown include {m.group(1)!r}")
        return fn(*m.group(2).split())

    out = _INCLUDE.sub(expand, text).strip()
    extra = appendix(ref) if appendix is not None else ""
    return f"{out}\n\n{extra}" if extra else out


class ReadRefArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    names: list[str] = Field(min_length=1, max_length=READ_REF_MAX, description=(
        "要读的参考的名字，照参考列表里写的原样写（例如 interaction、evoui/chart）"))


class ReadRefTool:
    """`read_ref`: the references of `skills` (the first one's refs may be named without the
    skill's prefix). Results over READ_REF_CHARS say which refs are left for the next call."""

    def __init__(self, skills: Sequence[Skill], includes: Includes = {},  # noqa: B006 - read only
                 appendix: Appendix | None = None, *, transform: Callable[[str], str] | None = None):
        self._skills = {s.name: s for s in skills}
        self._first = skills[0].name if skills else ""
        self._includes, self._appendix = includes, appendix
        self._transform = transform
        self.spec = spec("read_ref", "读方法说明（参考）。只读这一步实际要用的。", ReadRefArgs, "read")

    def names(self) -> list[str]:
        return [*(r for r in self._skills[self._first].refs)] if self._first else []

    def _split(self, name: str) -> tuple[Skill, str] | None:
        name = name.strip().removesuffix(".md")
        head, _, rest = name.partition("/")
        if head in self._skills and rest:
            return self._skills[head], rest
        first = self._skills.get(self._first)
        return (first, name) if first is not None else None

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return run_guarded(ReadRefArgs, args, self._run)

    def _run(self, a: ReadRefArgs) -> ToolResult:
        found: list[tuple[str, Skill, str]] = []
        unknown: list[str] = []
        for n in dict.fromkeys(a.names):
            hit = self._split(n)
            if hit is None or hit[1] not in hit[0].refs:
                unknown.append(n)
            else:
                found.append((n, *hit))
        if unknown:
            return fail(f"没有这些参考：{'、'.join(unknown)}。可以读的有：{'、'.join(self.names())}")
        out: list[str] = []
        left: list[str] = []
        for n, skill, ref in found:
            body = read_ref(skill, ref, self._includes, self._appendix)
            if self._transform is not None:
                body = self._transform(body)
            text = f"【{n}】\n" + body
            if left or (out and sum(map(len, out)) + len(text) > READ_REF_CHARS):
                left.append(n)
            else:
                out.append(text[:READ_REF_CHARS])
        more = f"\n\n（太长，这些下次再读：{'、'.join(left)}）" if left else ""
        return ToolResult(content="\n\n".join(out) + more)
