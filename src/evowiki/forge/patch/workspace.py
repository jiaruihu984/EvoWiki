"""The L2 coder's workspace over a real git worktree (design §4, §5.1; plan P3 T6). The first of
the three path checks of §5.1: every `write` / `edit` runs `paths.check_change` (the same function
the build and the landing step run) before anything touches disk. Names and semantics follow
Claude Code (Read/Edit/Write/Glob/Grep) and SWE-agent's ACI: syntax-checked edits refused with an
echo (the file stays as it was), windowed views, capped search, an explicit 没有结果.

What the model may see and touch:
- read / outline / grep / list cover only files tracked at the base commit plus the new files this
  workspace created. Anything else in the worktree (an untracked `.env`, `.git/**`, files a test
  run left behind) is invisible.
- write / edit refuse: paths that are not plain repo-relative POSIX paths or that leave the
  worktree (`..`, drives, symlinks, junctions); anything `check_change` refuses (deny list wins,
  existing tests read-only, simulator files, case variants, Windows-special names); new files git
  ignores (they would be invisible to `git diff`, so to the build check: e.g. a planted `.pyc`);
  more than `limits.L2_MAX_CHANGED_FILES` changed files; `.py` text that does not parse.

Fail closed: a git call that fails raises (GitError or PatchWorkspaceError); nothing is written
after a refused check."""

from __future__ import annotations

import ast
import fnmatch
import os
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from evowiki.forge import limits
from evowiki.forge.patch.git import Git, GitError
from evowiki.forge.patch.paths import Change, check_change

LINE_CUT = 500
GREP_LINE_CUT = 200
GREP_MAX = 50
LIST_MAX = 500
EMPTY = "没有结果"


class PatchWorkspaceError(ValueError):
    """A workspace operation was refused; the message is Chinese and meant for the model."""


def _is_link(p: Path) -> bool:
    return p.is_symlink() or bool(getattr(os.path, "isjunction", lambda _: False)(p))


@dataclass
class PatchWorkspace:
    root: Path                     # the candidate's worktree under the sibling forge directory
    git: Git
    base_commit: str
    base_files: frozenset[str]     # git ls-tree at base
    sim_files: frozenset[str]
    _created: set[str] = field(default_factory=set, repr=False)   # new files written here

    # --- paths -------------------------------------------------------------------------------

    def _rel(self, path: object) -> str:
        """A plain repo-relative POSIX path that stays inside the worktree, or refuse."""
        if not isinstance(path, str) or not path:
            raise PatchWorkspaceError("路径不能为空")
        p = PurePosixPath(path)
        if ("\\" in path or ":" in path or path.startswith("/") or p.is_absolute()
                or any(part in ("", ".", "..") for part in path.split("/"))
                or any(ord(c) < 32 for c in path)):
            raise PatchWorkspaceError(f"{path!r} 不是仓库内的相对路径")
        root = Path(self.root)
        cur = root
        for part in path.split("/"):
            cur = cur / part
            if _is_link(cur):
                raise PatchWorkspaceError(f"{path} 经过链接，不能读写")
        try:
            inside = (root / path).resolve().is_relative_to(root.resolve())
        except OSError:
            inside = False
        if not inside:
            raise PatchWorkspaceError(f"{path!r} 不在仓库里")
        return path

    def _visible(self, path: str) -> bool:
        return path in self.base_files or path in self._created

    def _readable(self, path: str) -> Path:
        rel = self._rel(path)
        f = Path(self.root) / rel
        if not self._visible(rel):
            raise PatchWorkspaceError("只能读仓库里的文件")
        if not f.is_file():
            raise PatchWorkspaceError(f"没有文件 {rel}")
        return f

    @staticmethod
    def _text(f: Path, rel: str) -> str:
        try:
            with open(f, encoding="utf-8", newline="") as fh:
                return fh.read()
        except UnicodeDecodeError:
            raise PatchWorkspaceError(f"{rel} 不是 UTF-8 文本") from None
        except OSError as e:
            raise PatchWorkspaceError(f"读不了 {rel}：{type(e).__name__}") from None

    # --- reading -----------------------------------------------------------------------------

    def read(self, path: str, offset: int = 1, limit: int = 200) -> str:
        f = self._readable(path)
        if offset < 1 or limit < 1:
            raise PatchWorkspaceError("offset 和 limit 必须 >= 1")
        lines = self._text(f, path).splitlines()
        if lines and offset > len(lines):
            raise PatchWorkspaceError(f"{path} 只有 {len(lines)} 行，offset={offset} 超出范围")
        return "\n".join(f"{n:>6}\t{line[:LINE_CUT]}"
                         for n, line in enumerate(lines[offset - 1: offset - 1 + limit], offset))

    def list(self, pattern: str = "**/*.py") -> str:
        """git-tracked files (at base) and the new files written here, matched with fnmatch."""
        root = Path(self.root)
        names = sorted(n for n in self.base_files | self._created
                       if fnmatch.fnmatchcase(n, pattern) and (root / n).is_file())
        if not names:
            return EMPTY
        if len(names) > LIST_MAX:
            return "\n".join(names[:LIST_MAX] + [f"……（共 {len(names)} 个，只显示前 {LIST_MAX} 个）"])
        return "\n".join(names)

    def grep(self, pattern: str) -> str:
        """`git grep -n -I -E` over the visible files; at most 50 hits."""
        # Git.grep raises on any exit other than 0/1; `--untracked` brings in the new files,
        # the filter below drops every untracked file this workspace did not write.
        try:
            out = self.git.grep(pattern, max_hits=10**9)
        except GitError:
            raise PatchWorkspaceError("正则不合法（git grep 用扩展正则），或搜索失败") from None
        hits: list[str] = []
        for line in out.splitlines():
            path, sep, rest = line.partition(":")
            if sep and self._visible(path):
                hits.append(f"{path}:{rest[:GREP_LINE_CUT]}")
        if not hits:
            return EMPTY
        if len(hits) > GREP_MAX:
            return "\n".join(hits[:GREP_MAX] + [f"……还有更多结果（共 {len(hits)} 条，只显示前 {GREP_MAX} 条）"])
        return "\n".join(hits)

    def outline(self, path: str) -> str:
        """Classes and functions with line numbers (methods indented)."""
        f = self._readable(path)
        if not path.endswith(".py"):
            raise PatchWorkspaceError("outline 只能看 .py 文件")
        try:
            tree = ast.parse(self._text(f, path), filename=path)
        except (SyntaxError, ValueError):
            raise PatchWorkspaceError(f"{path} 无法解析") from None
        out: list[str] = []

        def walk(body: list[ast.stmt], depth: int) -> None:
            for node in body:
                pad = "    " * depth
                if isinstance(node, ast.ClassDef):
                    out.append(f"{pad}{node.lineno}: class {node.name}")
                    walk(node.body, depth + 1)
                elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                    kw = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
                    out.append(f"{pad}{node.lineno}: {kw} {node.name}({ast.unparse(node.args)})")

        walk(tree.body, 0)
        return "\n".join(out) if out else EMPTY

    def changes(self) -> list[Change]:
        """`git diff --raw` from the base to the worktree, untracked (not ignored) files included."""
        return self.git.diff_raw(self.base_commit)

    # --- writing -----------------------------------------------------------------------------

    def _ignored(self, rel: str) -> bool:
        out = self.git.run("check-ignore", "-v", "-n", "--", rel, check=False)
        line = out.splitlines()[0] if out.strip() else ""
        if not line:
            raise PatchWorkspaceError("无法确认这个文件是否被 git 忽略，不写")
        return not line.startswith("::")

    def _check(self, rel: str) -> None:
        """Edit-time check 1 of 3 (§5.1): the same `check_change` as the build and the landing."""
        status = "M" if rel in self.base_files else "A"
        reasons = check_change([Change(status, rel)], base_files=self.base_files,
                               sim_files=self.sim_files)
        if reasons:
            raise PatchWorkspaceError("；".join(reasons))
        current = {c.path for c in self.changes()}
        if rel not in current and len(current) >= limits.L2_MAX_CHANGED_FILES:
            raise PatchWorkspaceError(f"改动的文件会超过上限 {limits.L2_MAX_CHANGED_FILES} 个")
        if status == "A" and self._ignored(rel):
            raise PatchWorkspaceError(f"{rel} 会被 git 忽略（不会进入改动），不能写")

    def _save(self, rel: str, text: str) -> None:
        if rel.endswith(".py"):
            try:
                ast.parse(text, filename=rel)
            except SyntaxError as e:
                bad = (e.text or "").rstrip("\r\n")[:200]
                raise PatchWorkspaceError(
                    f"语法错误：{rel} 第 {e.lineno} 行：{e.msg}：{bad!r}。修改被拒绝，文件保持原样"
                ) from None
            except ValueError as e:
                raise PatchWorkspaceError(f"语法错误：{rel}：{e}。修改被拒绝，文件保持原样") from None
        f = Path(self.root) / rel
        if f.is_dir():
            raise PatchWorkspaceError(f"{rel} 是目录")
        try:
            f.parent.mkdir(parents=True, exist_ok=True)
            self._rel(rel)                                   # nothing became a link meanwhile
            with open(f, "w", encoding="utf-8", newline="") as fh:
                fh.write(text)
        except OSError as e:
            raise PatchWorkspaceError(f"写不了 {rel}：{type(e).__name__}") from None
        if rel not in self.base_files:
            self._created.add(rel)

    def write(self, path: str, text: str) -> None:
        rel = self._rel(path)
        self._check(rel)
        self._save(rel, text)

    def edit(self, path: str, old: str, new: str, replace_all: bool = False) -> None:
        rel = self._rel(path)
        self._check(rel)
        f = Path(self.root) / rel
        if not self._visible(rel) or not f.is_file():
            raise PatchWorkspaceError(f"没有文件 {rel}")
        if old == "":
            raise PatchWorkspaceError("要替换的文本不能为空")
        if old == new:
            raise PatchWorkspaceError("新旧文本相同，没有修改")
        text = self._text(f, rel)
        n = text.count(old)
        if n == 0:
            raise PatchWorkspaceError(f"在 {rel} 里找不到要替换的文本；先 read_file 看原文再改")
        if n > 1 and not replace_all:
            raise PatchWorkspaceError(f"要替换的文本在 {rel} 里出现了 {n} 处；"
                                      "加长上下文使它唯一，或设 replace_all")
        self._save(rel, text.replace(old, new) if replace_all else text.replace(old, new, 1))
