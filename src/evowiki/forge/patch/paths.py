"""The L2 change checks (design §2, §5.1, §5.2 stage 2; plan P3 T3).

`check_change` is the ONE function the edit tools (T6), the build (T9, on the final
`git diff --raw`) and the landing step (the supervisor, on the commit before `cherry-pick`) all call:
the three checks of §5.1. It returns the reasons a change is refused, `()` when it is allowed.

On top of the anchor `forge.policy.violation` (allow/deny lists; the deny list wins) it refuses:
- deletes and renames of existing files (only adds and modifies are allowed); copies are adds;
- type changes, symlinks, submodules and any mode other than a plain or executable file;
- paths Windows would read differently from git: non-ASCII, reserved device names, trailing dots
  or spaces, `~` (8.3 short names), `:` (streams), `.git*` components, control characters;
- case variants: on Windows `tests/Test_old.py` IS `tests/test_old.py`, so existing files and
  simulator files are compared case-insensitively, and an add that differs from an existing file
  only by case is refused;
- files the simulator imports, computed from the `ast` import graph (`simulator_files`);
- more than `limits.L2_MAX_CHANGED_FILES` changed files.

Fail closed: an unknown status or mode is refused, and a graph that cannot be built raises
`importgraph.GraphError` (the caller refuses the change)."""

import ast
import re
import sys
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from importlib.metadata import packages_distributions
from pathlib import Path

from evowiki.forge import limits, policy
from evowiki.forge.patch import importgraph

SIM_ROOTS = ("src/evowiki/simulator/", "simulator/")
_STATUSES = frozenset("AMDRCT")
_FILE_MODES = frozenset({"100644", "100755"})
_RESERVED = frozenset({"con", "prn", "aux", "nul", "clock$", "conin$", "conout$",
                       *(f"com{i}" for i in "0123456789¹²³"),
                       *(f"lpt{i}" for i in "0123456789¹²³")})
_BAD_CHARS = set('<>:"|?*\\~')


@dataclass(frozen=True)
class Change:
    status: str            # git --raw letter: A M D R C T
    path: str
    old_path: str | None = None
    new_mode: str = "100644"


def simulator_files(root: Path, g: Mapping[str, set[str]] | None = None) -> frozenset[str]:
    """Every repo file the simulator imports, transitively, plus the simulator's own files.
    Raises GraphError when the graph cannot be built or no simulator file is found (a moved
    simulator must not silently turn the deny set empty)."""
    g = g if g is not None else importgraph.graph(Path(root))
    roots = [f for f in g if f.startswith(SIM_ROOTS)]
    if not roots:
        raise importgraph.GraphError("找不到模拟器的文件，无法确定它引用了哪些文件")
    return importgraph.closure(roots, g)


def _unsafe(path: object) -> str:
    """Why `path` is not a plain, unambiguous repo-relative path on every OS, or ""."""
    if not isinstance(path, str) or not path:
        return f"{path!r} 不是仓库内的相对路径"
    if not path.isascii():
        return f"{path!r} 只能用 ASCII 字符"
    if any(ord(ch) < 32 or ord(ch) == 127 or ch in _BAD_CHARS for ch in path):
        return f"{path!r} 含有不允许的字符"
    for part in path.split("/"):
        low = part.lower()
        if part in ("", ".", "..") or part != part.strip() or part.endswith("."):
            return f"{path!r} 不是仓库内的相对路径"
        if low.startswith(".git"):
            return f"{path!r} 不能修改 git 的配置文件"
        if low.split(".", 1)[0].rstrip() in _RESERVED:
            return f"{path!r} 用了 Windows 的保留名"
    return ""


def _path_problems(p: str, base_ci: Mapping[str, str], sim_ci: frozenset[str]) -> list[str]:
    bad = _unsafe(p)
    if bad:
        return [bad]
    key = p.lower()
    existing = base_ci.get(key)
    if existing is not None and existing != p:
        return [f"{p} 和已有文件 {existing} 只差大小写"]
    exists = existing is not None
    why = policy.violation(p, exists=exists) or (policy.violation(key, exists=exists)
                                                  if key != p else "")
    if why:
        return [why]
    if key.startswith("tests/") and (key.endswith("/conftest.py") or
                                     not re.fullmatch(r"tests/forge_[a-z0-9_]+/[a-z0-9_]+\.py", key)):
        return [f"{p} 不行：新测试只能放在 tests/forge_<短名>/ 下，不能是 conftest，不能影响别的测试和评测"]
    if key in sim_ci:
        return [f"{p} 被模拟器引用，不能修改"]
    return []


def check_change(changes: Sequence[Change], *, base_files: frozenset[str],
                 sim_files: frozenset[str]) -> tuple[str, ...]:
    """Reasons the change set is refused (deduplicated, in order); () = allowed.
    `base_files` = files in the base commit; `sim_files` = `simulator_files(...)` of the base."""
    out: list[str] = []
    if len(changes) > limits.L2_MAX_CHANGED_FILES:
        out.append(f"改动了 {len(changes)} 个文件，超过上限 {limits.L2_MAX_CHANGED_FILES}")
    base_ci = {p.lower(): p for p in base_files}
    sim_ci = frozenset(p.lower() for p in sim_files)
    for c in changes:
        if not isinstance(c, Change) or c.status not in _STATUSES:
            out.append(f"{getattr(c, 'path', c)!r} 的改动类型不支持")
            continue
        if c.status == "T" or (c.status != "D" and c.new_mode not in _FILE_MODES):
            out.append(f"{c.path!r} 不能改成链接、子模块或特殊文件")
            continue
        if c.status in ("R", "C") and not c.old_path:
            out.append(f"{c.path!r} 的改名或复制缺少原路径")
            continue
        removed = c.path if c.status == "D" else c.old_path if c.status == "R" else None
        if removed is not None and isinstance(removed, str) and removed.lower() in base_ci:
            out.append(f"{removed} 是已有文件，不能删除或改名")
        touched = [c.path] + ([c.old_path] if c.status == "R" and c.old_path else [])
        for p in touched:
            out.extend(_path_problems(p, base_ci, sim_ci))
    return tuple(dict.fromkeys(out))


# --- what the added lines may not reach ----------------------------------------------------------

# A file in an allowed folder can still reach what grades it, or leave the machine, by what it says. A plain
# reading of the added lines: dynamic tricks stay for the reviewer, who sees the diff before anything lands.
_REACH: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("评分和门控的代码", re.compile(r"evowiki\.harness\.(gate|judge|judge_prompt|anchors|keys|improver_prompt|patch_gate)\b"
                            r"|evowiki\.harness import .*\b(gate|judge|judge_prompt|anchors|keys|improver_prompt)\b"
                            r"|evowiki\.(forge|evaluation|experiments|rsib)\b|evowiki\.teach\.anchors\b"
                            r"|llm_cases[./\\]|JUDGE_CALIBRATED")),
    ("秘钥文件", re.compile(r"""['"/\\]\.env\b|EVOWIKI_ADMIN_DATABASE_URL|_API_KEY""")),
    ("网络", re.compile(r"^\+\s*(import|from)\s+(socket|http|urllib|httpx|requests|aiohttp|ftplib|smtplib)\b")),
    ("别的程序", re.compile(r"^\+\s*(import|from)\s+(subprocess|ctypes|winreg|_winapi)\b|\bos\.(system|popen)\(")),
)


def reach_hits(diff_text: str) -> tuple[str, ...]:
    """What the diff's added lines reach that a patch may not (labels, never the text)."""
    out: list[str] = []
    for line in diff_text.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            out.extend(f"改动里用到了{name}" for name, pat in _REACH if pat.search(line))
    return tuple(dict.fromkeys(out))


# --- secrets -------------------------------------------------------------------------------------

_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("Anthropic key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}")),
    ("sk- key", re.compile(r"sk-[A-Za-z0-9_\-]{20,}")),
    ("Tavily key", re.compile(r"tvly-[A-Za-z0-9]{16,}")),
    ("AWS key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("assigned secret", re.compile(
        r"(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*['\"][^'\"]{12,}['\"]")),
)


def secret_hits(diff_text: str, env_values: Iterable[str]) -> tuple[str, ...]:
    """Labels of added diff lines that look like a secret or contain a `.env` value. A label names
    the line and the pattern, never the value. Every line starting with `+` is scanned, the
    `+++` header lines included (a content line starting with `++` looks like one)."""
    values = [v for v in env_values if v]
    out: list[str] = []
    for n, line in enumerate(diff_text.splitlines(), start=1):
        if not line.startswith("+"):
            continue
        for name, pat in _SECRET_PATTERNS:
            if pat.search(line):
                out.append(f"第 {n} 行像是秘钥（{name}）")
        if any(v in line for v in values):
            out.append(f"第 {n} 行包含 .env 里的值")
    return tuple(dict.fromkeys(out))


def env_secret_values(env_file: Path) -> tuple[str, ...]:
    """Values of at least 12 characters in `env_file` (`KEY=VALUE` lines), for the literal scan.
    Kept in memory only. A missing file has no values; an unreadable one raises."""
    env_file = Path(env_file)
    if not env_file.exists():
        return ()
    out: list[str] = []
    for raw in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        line = line.removeprefix("export ")
        value = line.split("=", 1)[1].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if len(value) >= 12:
            out.append(value)
    return tuple(dict.fromkeys(out))


# --- dependencies --------------------------------------------------------------------------------

_REQ_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def declared_distributions(pyproject: Path) -> frozenset[str]:
    """Import names provided by the dependencies `pyproject` declares ([project].dependencies,
    optional-dependencies, [dependency-groups]). Installed metadata maps a distribution to its
    import names (PyYAML -> yaml); a declared name with no metadata counts as `name` with `_`."""
    data = tomllib.loads(Path(pyproject).read_text(encoding="utf-8"))
    project = data.get("project", {})
    reqs: list[object] = list(project.get("dependencies", []))
    for group in project.get("optional-dependencies", {}).values():
        reqs.extend(group)
    for group in data.get("dependency-groups", {}).values():
        reqs.extend(group)
    declared: set[str] = set()
    for r in reqs:
        if isinstance(r, str) and (m := _REQ_NAME.match(r)):
            declared.add(_norm(m.group(1)))
    out = {d.replace("-", "_") for d in declared}
    for imp, dists in packages_distributions().items():
        if any(_norm(d) in declared for d in dists):
            out.add(imp)
    return frozenset(out)


def _top_imports(path: str, text: str) -> set[str]:
    try:
        tree = ast.parse(text, filename=path)
    except (SyntaxError, ValueError) as e:
        raise importgraph.GraphError(f"{path} 无法解析：{type(e).__name__}") from e
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                out.add(node.module.split(".")[0])
        elif isinstance(node, ast.Call):
            f = node.func
            fname = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) \
                else ""
            if (fname in ("import_module", "__import__") and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)
                    and node.args[0].value and not node.args[0].value.startswith(".")):
                out.add(node.args[0].value.split(".")[0])
    return out


def foreign_imports(texts: Mapping[str, str], *, declared: frozenset[str]) -> tuple[str, ...]:
    """Top-level modules the changed `.py` texts import that are neither stdlib, `evowiki`,
    `tests`, relative, nor in `declared` (a patch may not add dependencies). Sorted, unique.
    Raises GraphError when a text does not parse."""
    found: set[str] = set()
    for path, text in texts.items():
        if path.endswith(".py"):
            found |= _top_imports(path, text)
    allowed = set(sys.stdlib_module_names) | {"evowiki", "tests"} | set(declared)
    return tuple(sorted(n for n in found if n and n not in allowed))
