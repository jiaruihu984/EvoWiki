"""The forge's only git caller (plan P3 T3). argv lists, never a shell; external diff drivers and
textconv off (a .gitattributes cannot run commands: it is not an allowed path anyway); hooks off
(`core.hooksPath` = the null device); every inherited `GIT_*` variable dropped (no `GIT_DIR`,
`GIT_EXTERNAL_DIFF`, ...); a ref or sha that could be read as an option is refused. A fixed author
per forge commit (aider: one attributed commit per change, undo = git revert).

Diffs and patches are produced and applied by git itself (R3); this module parses only git's
machine output (`--raw -z`, `-z` listings)."""

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from evowiki.forge.patch.paths import Change

_SAFE = ("-c", "core.fsmonitor=false", "-c", f"core.hooksPath={os.devnull}", "-c",
         "diff.external=", "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false",
         "-c", "protocol.allow=never", "-c", "core.quotePath=false")
_AUTHOR = re.compile(r"^([^<>\n]+?) <([^<>\s]+)>$")
_REF_BAD = re.compile(r"[\s\x00-\x1f\x7f]")
_LINK_MODE = "120000"


class GitError(RuntimeError):
    """A git call failed, timed out, or was refused before it ran. Callers fail closed."""


def _ref(ref: str) -> str:
    if not isinstance(ref, str) or not ref or ref.startswith("-") or _REF_BAD.search(ref):
        raise GitError(f"不是合法的 git 引用：{ref!r}")
    return ref


def _author(author: str) -> tuple[str, str]:
    m = _AUTHOR.match(author)
    if not m:
        raise GitError(f"作者格式应为 'Name <email>'：{author!r}")
    return m.group(1), m.group(2)


def _is_link(p: Path) -> bool:
    return p.is_symlink() or bool(getattr(os.path, "isjunction", lambda _: False)(p))


@dataclass(frozen=True)
class Git:
    root: Path                         # any checkout or worktree of the repo

    def run(self, *args: str, check: bool = True, timeout: float = 120.0) -> str:
        rc, out, err = self._rc(*args, timeout=timeout)
        if check and rc != 0:
            raise GitError(f"git {args[0] if args else ''} failed: {err.strip()[:500]}")
        return out

    def _rc(self, *args: str, timeout: float = 120.0) -> tuple[int, str, str]:
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
        env["GIT_TERMINAL_PROMPT"] = "0"
        argv = ["git", "-C", str(self.root), *_SAFE, *args]
        try:
            r = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=timeout, env=env, check=False)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise GitError(f"git {args[0] if args else ''} 无法运行：{type(e).__name__}") from e
        return r.returncode, r.stdout, r.stderr

    # --- refs --------------------------------------------------------------------------------

    def head(self) -> str:
        return self.rev_parse("HEAD")

    def rev_parse(self, ref: str) -> str:
        return self.run("rev-parse", "--verify", "--end-of-options",
                        f"{_ref(ref)}^{{commit}}").strip()

    def merge_base(self, a: str, b: str) -> str:
        return self.run("merge-base", "--end-of-options", _ref(a), _ref(b)).strip()

    def commits_since(self, base: str, head: str = "HEAD") -> list[tuple[str, str]]:
        """(sha, message) of `base..head`, oldest first."""
        out = self.run("log", "--reverse", "--format=%H%x00%B%x01", "--end-of-options",
                       f"{_ref(base)}..{_ref(head)}")
        res: list[tuple[str, str]] = []
        for rec in out.split("\x01"):
            rec = rec.lstrip("\n")
            if not rec:
                continue
            sha, _, msg = rec.partition("\x00")
            res.append((sha.strip(), msg.rstrip("\n")))
        return res

    # --- worktrees ---------------------------------------------------------------------------

    def worktree_add(self, path: Path, *, branch: str, base: str) -> "Git":
        """New branch `branch` at `base`, checked out at `path`."""
        self.run("worktree", "add", "-q", "-b", _ref(branch), "--", str(path), _ref(base))
        return Git(Path(path))

    def worktree_remove(self, path: Path) -> None:
        """Remove the worktree (--force); the branch is kept."""
        self.run("worktree", "remove", "--force", "--", str(path))

    # --- reading -----------------------------------------------------------------------------

    def ls_files(self, commit: str = "HEAD") -> frozenset[str]:
        out = self.run("ls-tree", "-r", "-z", "--name-only", "--full-tree", "--end-of-options",
                       _ref(commit))
        return frozenset(p for p in out.split("\0") if p)

    def show(self, commit: str, path: str) -> str | None:
        """File text at `commit`, None if absent."""
        if not isinstance(path, str) or "\0" in path or "\n" in path:
            raise GitError(f"不是合法的路径：{path!r}")
        rc, out, _ = self._rc("cat-file", "blob", f"{_ref(commit)}:{path}")
        return out if rc == 0 else None

    def _untracked(self) -> list[str]:
        out = self.run("ls-files", "-z", "--others", "--exclude-standard", "--full-name")
        return [p for p in out.split("\0") if p]

    def diff_raw(self, base: str, head: str | None = None) -> list[Change]:
        """`git diff --raw -M` from `base` to `head`; head None = the worktree including untracked
        files (as `A`). For the worktree, a path that is or lies under a symlink/junction gets
        mode 120000, whatever git (core.symlinks=false on Windows) reports, so `check_change`
        refuses it."""
        args = ["diff", "--raw", "-z", "-M", "--no-ext-diff", "--no-textconv", "--no-color",
                "--end-of-options", _ref(base)]
        if head is not None:
            args.append(_ref(head))
        args.append("--")
        changes = _parse_raw(self.run(*args))
        if head is None:
            seen = {c.path for c in changes}
            changes += [Change("A", p) for p in self._untracked() if p not in seen]
            changes = [self._link_checked(c) for c in changes]
        return changes

    def _link_checked(self, c: Change) -> Change:
        if c.status == "D":
            return c
        cur = Path(self.root)
        for part in c.path.split("/"):
            cur = cur / part
            if _is_link(cur):
                return Change(c.status, c.path, c.old_path, _LINK_MODE)
        return c

    def diff(self, base: str, head: str | None = None) -> str:
        """Unified diff (--no-ext-diff --no-textconv); head None = worktree incl. untracked."""
        args = ["diff", "--no-ext-diff", "--no-textconv", "--no-color", "-M", "--end-of-options",
                _ref(base)]
        if head is not None:
            args.append(_ref(head))
        args.append("--")
        out = self.run(*args)
        if head is None:
            for p in self._untracked():
                rc, d, err = self._rc("diff", "--no-index", "--no-ext-diff", "--no-textconv",
                                      "--no-color", "--", os.devnull, p)
                if rc not in (0, 1):
                    raise GitError(f"git diff --no-index failed: {err.strip()[:500]}")
                out += d
        return out

    def grep(self, pattern: str, *, max_hits: int = 50) -> str:
        """`git grep -n -I -E` over tracked and untracked files; "" when nothing matches."""
        rc, out, err = self._rc("grep", "-n", "-I", "--untracked", "--no-color", "-E", "-e",
                                pattern, "--")
        if rc == 1:
            return ""
        if rc != 0:
            raise GitError(f"git grep failed: {err.strip()[:500]}")
        lines = out.splitlines()
        if len(lines) > max_hits:
            lines = lines[:max_hits] + [f"……（共 {len(lines)} 条，只显示前 {max_hits} 条）"]
        return "\n".join(lines)

    def is_clean(self) -> bool:
        return self.run("status", "--porcelain", "--untracked-files=all") == ""

    # --- writing -----------------------------------------------------------------------------

    def add_all(self) -> None:
        self.run("add", "-A", "--", ".")

    def _who(self, author: str) -> tuple[str, ...]:
        name, email = _author(author)
        return ("-c", f"user.name={name}", "-c", f"user.email={email}")

    def commit(self, message: str, *, author: str) -> str:
        """One attributed commit; returns its sha."""
        self.run(*self._who(author), "commit", "-q", "--no-verify", "-m", message,
                 f"--author={author}")
        return self.head()

    def cherry_pick(self, sha: str) -> bool:
        """False on conflict (or an empty pick): aborted, tree clean."""
        rc, _, err = self._rc("cherry-pick", "-x", "--end-of-options", _ref(sha))
        if rc == 0:
            return True
        arc, _, aerr = self._rc("cherry-pick", "--abort")
        if arc != 0:
            raise GitError(f"git cherry-pick failed and could not be aborted: "
                           f"{err.strip()[:300]} / {aerr.strip()[:200]}")
        return False

    def revert(self, sha: str, message: str, *, author: str) -> str | None:
        """Revert `sha` as one attributed commit carrying `message`; None on conflict (aborted)."""
        who = self._who(author)
        rc, _, err = self._rc(*who, "revert", "--no-edit", "--end-of-options", _ref(sha))
        if rc != 0:
            arc, _, aerr = self._rc("revert", "--abort")
            if arc != 0:
                raise GitError(f"git revert failed and could not be aborted: "
                               f"{err.strip()[:300]} / {aerr.strip()[:200]}")
            return None
        self.run(*who, "commit", "-q", "--amend", "--no-verify", "-m", message,
                 f"--author={author}")
        return self.head()

    def reset_hard(self, ref: str) -> None:
        self.run("reset", "-q", "--hard", _ref(ref))


def _parse_raw(out: str) -> list[Change]:
    """Parse `git diff --raw -z`: ':<old_mode> <new_mode> <a> <b> <S>\\0path\\0[path2\\0]'."""
    toks = out.split("\0")
    res: list[Change] = []
    i = 0
    while i < len(toks):
        meta = toks[i]
        if not meta:
            i += 1
            continue
        if not meta.startswith(":"):
            raise GitError(f"无法解析 git diff --raw 的输出：{meta[:80]!r}")
        fields = meta[1:].split()
        if len(fields) != 5:
            raise GitError(f"无法解析 git diff --raw 的输出：{meta[:80]!r}")
        new_mode, status = fields[1], fields[4][:1]
        if status in ("R", "C"):
            if i + 2 >= len(toks):
                raise GitError("git diff --raw 的改名记录不完整")
            res.append(Change(status, toks[i + 2], old_path=toks[i + 1], new_mode=new_mode))
            i += 3
        else:
            if i + 1 >= len(toks):
                raise GitError("git diff --raw 的记录不完整")
            res.append(Change(status, toks[i + 1], new_mode=new_mode))
            i += 2
    return res
