"""The assistant's Skill catalog: the method text it loads on demand, and the tools that come
with it (design A4, B2; stage 3 T2/T3). The page writer's skills (write-page: entry text,
references with includes) are read by `agent.skills`; both live under `src/evowiki/skills/`.
Unifying the two loaders is a follow-up (whoever merged first owns the loader: the writer's).

A Skill is a directory `skills/<key>/` with a `SKILL.md` (YAML frontmatter + the rules) and a
`references/` folder of one-level reference files:

    ---
    key: plan-goal              # what load_skill takes
    name: 目标规划               # the catalog's name
    when: 想学新东西、改分块…    # the catalog's one line
    order: 10                   # catalog order (tool order follows it: stable request prefixes)
    tools: [new_goal, ...]      # registered once the Skill is loaded
    refs:                       # references/<name>.md; tools registered once it is read
      graph: {when: 改图谱时, tools: [read_graph, ...]}
    turn_rule: wrong-answer     # optional: a reference placed in the user message of a turn whose
    ---                         #   code signal holds (the loop decides; BRIDGE)
    rules…

Load-time checks (fail closed, `SkillError`): the frontmatter has only these keys (a Skill can
never carry a tier, a permission or a grant: tiers are code, `agent.tools.ToolTier`); refs are one
level deep; a reference never names another reference's path; every tool named exists in the
injected registry. `version` is a content hash (hash8) of the Skill's files, each reference has its
own; the trace and `prompt_version` carry them.

The tools: `load_skill(skills)` returns the Skills' rules, `read_ref(path)` one reference body
(loading its Skill). Both are read tier, make no model call, and their result is a function of the
files and of what this turn already read (`Activation`): a repeat read says where it was read. A
successful call names what it loaded in `ToolResult.loaded`, part of its recorded result, so which
Skills are active is derived from logged results, never from mutable state outside them.

`activate(...)` is the one place that turns "active Skills + references read" into the ordered
list of tool names a request offers."""

import hashlib
import re
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from evowiki.agent.tool_helpers import fail, run_guarded, spec
from evowiki.agent.tools import ToolContext, ToolResult

SKILLS_ROOT = Path(__file__).resolve().parent.parent / "skills"
SKILL_FILE = "SKILL.md"
REFS_DIR = "references"
_KEYS = frozenset({"key", "name", "when", "order", "tools", "refs", "turn_rule"})
_REF_KEYS = frozenset({"when", "tools"})
_NAME = re.compile(r"^[a-z][a-z0-9-]{0,40}$")
LOAD_SKILL = "load_skill"
READ_REF = "read_ref"


class SkillError(ValueError):
    """A Skill directory that breaks the format or the load-time checks."""


def library_version(lib: "SkillLibrary") -> str:
    """One hash over every Skill's version: what the trace records per turn."""
    return hash8(*(f"{s.key}@{s.version}".encode() for s in lib.skills))


def hash8(*parts: bytes) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(len(p).to_bytes(8, "big"))
        h.update(p)
    return h.hexdigest()[:8]


@dataclass(frozen=True)
class Ref:
    skill: str
    name: str
    when: str
    tools: tuple[str, ...]
    body: str
    version: str

    @property
    def path(self) -> str:
        return f"{self.skill}/{self.name}"


@dataclass(frozen=True)
class Skill:
    key: str
    name: str
    when: str
    order: int
    rules: str
    tools: tuple[str, ...]
    refs: Mapping[str, Ref]
    version: str
    turn_rule: str | None = None          # a reference name


@dataclass(frozen=True)
class SkillLibrary:
    skills: tuple[Skill, ...]             # catalog order

    def keys(self) -> tuple[str, ...]:
        return tuple(s.key for s in self.skills)

    def get(self, key: str) -> Skill | None:
        return next((s for s in self.skills if s.key == key), None)

    def ref(self, path: str) -> Ref | None:
        key, _, name = path.partition("/")
        s = self.get(key)
        return None if s is None else s.refs.get(name)

    def paths(self) -> tuple[str, ...]:
        return tuple(r.path for s in self.skills for r in s.refs.values())

    def catalog(self) -> str:
        """One line per Skill: what the core prompt carries, with the tools it brings (a tool not
        offered yet still exists: the model must know, or it says it cannot); a reference that
        brings tools is named with them, so a change behind it costs one read_ref, not a load and
        then a read."""
        lines = []
        for s in self.skills:
            refs = "；".join(f"{r.path}（{'、'.join(r.tools)}）" for r in s.refs.values() if r.tools)
            tools = "、".join(s.tools)
            lines.append(f"- {s.key}（{s.name}）：{s.when}" + (f"。工具：{tools}" if tools else "")
                         + (f"。参考：{refs}" if refs else ""))
        return "\n".join(lines)

    def ordered(self, keys: Collection[str]) -> tuple[str, ...]:
        return tuple(s.key for s in self.skills if s.key in keys)

    def versions(self, keys: Collection[str], refs: Collection[str] = ()) -> dict[str, str]:
        """{key: version} of the Skills, {path: version} of the references, in catalog order."""
        out = {s.key: s.version for s in self.skills if s.key in keys}
        for s in self.skills:
            for r in s.refs.values():
                if r.path in refs:
                    out[r.path] = r.version
        return out

    def all_tools(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(t for s in self.skills
                                   for t in (*s.tools, *(x for r in s.refs.values()
                                                         for x in r.tools))))


def _frontmatter(text: str, where: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---\n"):
        raise SkillError(f"{where}: no frontmatter")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise SkillError(f"{where}: unterminated frontmatter")
    try:
        meta = yaml.safe_load(text[4:end]) or {}
    except yaml.YAMLError as e:
        raise SkillError(f"{where}: bad frontmatter: {e}") from e
    if not isinstance(meta, dict):
        raise SkillError(f"{where}: frontmatter is not a mapping")
    return meta, text[end + 5:].strip()


def _names(value: Any, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise SkillError(f"{where}: tools must be a list of names")
    return tuple(dict.fromkeys(value))


def _text(meta: Mapping[str, Any], k: str, where: str) -> str:
    v = meta.get(k)
    if not isinstance(v, str) or not v.strip():
        raise SkillError(f"{where}: `{k}` is required")
    return " ".join(v.split())


def _lf(b: bytes) -> bytes:
    return b.replace(b"\r\n", b"\n")


def load_skill_dir(d: Path, registry: Collection[str] | None = None) -> Skill:
    where = str(d.name)
    f = d / SKILL_FILE
    if not f.is_file():
        raise SkillError(f"{where}: no {SKILL_FILE}")
    raw = _lf(f.read_bytes())         # the hash ignores the checkout's line ends
    meta, rules = _frontmatter(raw.decode("utf-8"), where)
    unknown = set(meta) - _KEYS
    if unknown:     # tiers, permissions and grants are code only (B3.1 #2)
        raise SkillError(f"{where}: unknown frontmatter keys {sorted(unknown)}")
    key = _text(meta, "key", where)
    if not _NAME.fullmatch(key) or key != d.name:
        raise SkillError(f"{where}: key must be the directory name")
    order = meta.get("order", 1000)
    if not isinstance(order, int):
        raise SkillError(f"{where}: order must be an integer")
    tools = _names(meta.get("tools"), where)
    refs_meta = meta.get("refs") or {}
    if not isinstance(refs_meta, dict):
        raise SkillError(f"{where}: refs must be a mapping")
    refs: dict[str, Ref] = {}
    parts: list[bytes] = [raw]
    for name in sorted(refs_meta):
        r = refs_meta[name] or {}
        rw = f"{where}/{name}"
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise SkillError(f"{rw}: a reference name is one level (letters, digits, -)")
        if not isinstance(r, dict) or set(r) - _REF_KEYS:
            raise SkillError(f"{rw}: a reference has only `when` and `tools`")
        rf = d / REFS_DIR / f"{name}.md"
        if not rf.is_file():
            raise SkillError(f"{rw}: no {REFS_DIR}/{name}.md")
        body_raw = _lf(rf.read_bytes())
        parts += [name.encode(), body_raw]
        refs[name] = Ref(skill=key, name=name, when=_text(r, "when", rw),
                         tools=_names(r.get("tools"), rw),
                         body=body_raw.decode("utf-8").strip(),
                         version=hash8(body_raw))
    extra = d / REFS_DIR
    if extra.is_dir():
        stray = sorted(p.name for p in extra.iterdir()
                       if p.suffix != ".md" or p.stem not in refs or p.is_dir())
        if stray:
            raise SkillError(f"{where}: references not declared (or nested): {stray}")
    turn_rule = meta.get("turn_rule")
    if turn_rule is not None and turn_rule not in refs:
        raise SkillError(f"{where}: turn_rule names no reference")
    skill = Skill(key=key, name=_text(meta, "name", where), when=_text(meta, "when", where),
                  order=order, rules=rules, tools=tools, refs=refs, version=hash8(*parts),
                  turn_rule=turn_rule)
    if registry is not None:
        named = (*skill.tools, *(t for r in refs.values() for t in r.tools))
        missing = sorted(set(named) - set(registry))
        if missing:
            raise SkillError(f"{where}: tools not in the registry: {missing}")
    return skill


def _check_flat(skills: Sequence[Skill]) -> None:
    """A reference never names another reference's path (one level, P4)."""
    paths = [r.path for s in skills for r in s.refs.values()]
    for s in skills:
        for r in s.refs.values():
            for p in paths:
                if p != r.path and (p in r.body or f"{REFS_DIR}/" in r.body):
                    raise SkillError(f"{r.path}: names another reference ({p})")


def load_library(root: Path = SKILLS_ROOT, *, registry: Collection[str] | None = None,
                 keys: Collection[str] | None = None) -> SkillLibrary:
    """Every Skill under `root` (or only `keys`), checked; catalog order = (order, key)."""
    dirs = sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith(("_", ".")))
    skills = [load_skill_dir(d, registry) for d in dirs if keys is None or d.name in keys]
    if keys is not None and set(keys) - {s.key for s in skills}:
        raise SkillError(f"unknown skills: {sorted(set(keys) - {s.key for s in skills})}")
    _check_flat(skills)
    return SkillLibrary(tuple(sorted(skills, key=lambda s: (s.order, s.key))))


# ---- what a turn has loaded ----

@dataclass
class Activation:
    """The Skills and references of one turn. `carried`: still active from earlier turns (their
    rules in the system prompt); `preloaded`: by this turn's code signals (rules in the system
    prompt too); `carried_refs`: references read in an earlier turn (named in the system prompt,
    their tools registered; a read gives the body again); `preloaded_refs`: references whose bodies
    the system prompt carries this turn; and what this turn's successful load_skill / read_ref calls
    added (`note`, fed the recorded results in order)."""

    library: SkillLibrary
    carried: tuple[str, ...] = ()
    preloaded: tuple[str, ...] = ()
    carried_refs: tuple[str, ...] = ()
    preloaded_refs: tuple[str, ...] = ()
    loaded: list[str] = field(default_factory=list)         # skill keys, this turn
    read: dict[str, int] = field(default_factory=dict)      # ref path -> the call that read it
    calls: int = 0

    def skills(self) -> tuple[str, ...]:
        return self.library.ordered({
            *self.carried, *self.preloaded, *self.loaded,
            *(p.partition("/")[0] for p in (*self.read, *self.carried_refs,
                                            *self.preloaded_refs))})

    def ruled(self) -> frozenset[str]:
        """The Skills whose rules the model has: in the system prompt, or a load_skill result."""
        return frozenset({*self.carried, *self.preloaded, *self.loaded})

    def refs(self) -> tuple[str, ...]:
        have = {*self.read, *self.carried_refs, *self.preloaded_refs}
        return tuple(p for p in self.library.paths() if p in have)

    def note(self, result: ToolResult) -> None:
        """One recorded load_skill / read_ref result (the loop calls it from after_tool)."""
        self.calls += 1
        if result.error is not None:
            return
        for item in result.loaded:
            if "/" in item:
                self.read.setdefault(item, self.calls)
            elif item not in self.loaded:
                self.loaded.append(item)

    def where_read(self, path: str) -> str | None:
        if path in self.preloaded_refs:
            return "这份参考已经在系统提示里了，照着它做就行"
        n = self.read.get(path)
        return None if n is None else f"这份参考已在这一轮第 {n} 次载入时读过，内容还在上面"


@dataclass(frozen=True)
class ToolSet:
    """What one request offers: tool names in order, and the versions of the Skills and
    references behind them (the trace and `prompt_version` record these)."""
    names: tuple[str, ...]
    versions: Mapping[str, str]


def activate(library: SkillLibrary, *, available: Sequence[str], core: Sequence[str],
             skills: Collection[str], refs: Collection[str],
             also: Sequence[str] = ()) -> ToolSet:
    """The tools a request offers: the core ones, then each active Skill's tools and its read
    references' tools in catalog order, then `also` (e.g. undo while something can be undone) -
    each only if the turn built it (`available`: after the harness pack's toolset), deduplicated."""
    have = set(available)
    names: list[str] = [n for n in core if n in have]
    for s in library.skills:
        if s.key not in skills:
            continue
        names += s.tools
        for r in s.refs.values():
            if r.path in refs:
                names += r.tools
    names += also
    ordered = tuple(dict.fromkeys(n for n in names if n in have))
    return ToolSet(ordered, library.versions(skills, refs))


# ---- the two tools ----

class LoadSkillArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    skills: list[str] = Field(min_length=1, max_length=4, description="要载入的技能 key")


class ReadRefArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=3, max_length=80, description="参考的路径，如 edit-wiki/graph")


LOAD_DESC = "载入一个或几个技能：返回它们的做法，它们的工具从下一次调用开始可以用。"
READ_DESC = "读一个技能的参考（例如 edit-wiki/graph）：返回参考正文，它的工具从下一次调用开始可以用。"


def _skill_text(s: Skill) -> str:
    refs = "\n".join(f"- {r.path}：{r.when}" for r in s.refs.values())
    more = f"\n可以读的参考（read_ref）：\n{refs}" if refs else ""
    return f"【技能·{s.name}】\n{s.rules}{more}"


class LoadSkillTool:
    def __init__(self, activation: Callable[[], Activation]):
        self._act = activation
        self.spec = spec(LOAD_SKILL, LOAD_DESC, LoadSkillArgs, "read", tier="read")

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return run_guarded(LoadSkillArgs, args, self._run)

    def _run(self, a: LoadSkillArgs) -> ToolResult:
        act = self._act()
        lib = act.library
        keys = list(dict.fromkeys(k.strip() for k in a.skills))
        unknown = [k for k in keys if lib.get(k) is None]
        if unknown:       # nothing is loaded: never a silent partial
            return fail(f"没有这些技能：{'、'.join(unknown)}；可以载入的：{'、'.join(lib.keys())}")
        ruled = act.ruled()          # rules already given (a read_ref alone gives none)
        parts = [f"【技能·{s.name}】已经载入了，照上面的做法做" if s.key in ruled else _skill_text(s)
                 for s in (lib.get(k) for k in keys) if s is not None]
        return ToolResult(content="\n\n".join(parts), loaded=tuple(keys),
                          summary="、".join(k for k in keys))


class ReadRefTool:
    def __init__(self, activation: Callable[[], Activation]):
        self._act = activation
        self.spec = spec(READ_REF, READ_DESC, ReadRefArgs, "read", tier="read")

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return run_guarded(ReadRefArgs, args, self._run)

    def _run(self, a: ReadRefArgs) -> ToolResult:
        act = self._act()
        path = a.path.strip().removesuffix(".md")
        ref = act.library.ref(path)
        if ref is None:
            return fail(f"没有这份参考：{path}；可以读的：{'、'.join(act.library.paths())}")
        seen = act.where_read(path)
        if seen is not None:
            return ToolResult(content=seen, loaded=(ref.path,), summary=ref.path)
        return ToolResult(content=f"【参考·{ref.path}】\n{ref.body}", loaded=(ref.path,),
                          summary=ref.path)
