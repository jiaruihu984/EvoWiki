"""The coder's workspace: an immutable in-memory dict of files (design 2026-10-22 §6.2). Every
change returns a new `Workspace`; nothing touches disk. Names, sizes and Python syntax are checked
on every write so a bad edit never lands (SWE-agent ACI: syntax-checked edits, capped search,
explicit empty result). Pure: no IO."""

import ast
import fnmatch
import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

import regex

from evowiki.forge import limits

LINE_CUT = 500
GREP_LINE_CUT = 200
EMPTY = "没有结果"


class WorkspaceError(ValueError):
    """A workspace operation was refused; the message is Chinese and meant for the model."""


def _check_name(path: str) -> None:
    if not any(re.fullmatch(p, path) for p in limits.WS_ALLOWED):
        raise WorkspaceError(f"文件名 {path!r} 不允许：只能是 xxx.py（小写字母、数字、下划线）、"
                             "schema.json、examples.json、README.md，且不能带目录")


def _check_syntax(path: str, text: str) -> None:
    if not path.endswith(".py"):
        return
    try:
        ast.parse(text, filename=path)
    except SyntaxError as e:
        raise WorkspaceError(f"语法错误（第 {e.lineno} 行）：{e.msg}；这次修改没有生效") from None


def _check_caps(files: Mapping[str, str]) -> None:
    if len(files) > limits.WS_MAX_FILES:
        raise WorkspaceError(f"文件数超过上限 {limits.WS_MAX_FILES}；这次修改没有生效")
    if sum(len(t.encode("utf-8")) for t in files.values()) > limits.WS_MAX_BYTES:
        raise WorkspaceError(f"工作区总大小超过 {limits.WS_MAX_BYTES >> 20} MB；这次修改没有生效")


@dataclass(frozen=True)
class Workspace:
    files: Mapping[str, str]

    @classmethod
    def of(cls, files: Mapping[str, str]) -> "Workspace":
        for name in files:
            _check_name(name)
        _check_caps(files)
        return cls(MappingProxyType(dict(files)))

    def _with(self, path: str, text: str) -> "Workspace":
        _check_name(path)
        _check_syntax(path, text)
        new = {**self.files, path: text}
        _check_caps(new)
        return Workspace(MappingProxyType(new))

    def write(self, path: str, text: str) -> "Workspace":
        return self._with(path, text)

    def edit(self, path: str, old: str, new: str, replace_all: bool = False) -> "Workspace":
        _check_name(path)
        if path not in self.files:
            raise WorkspaceError(f"没有文件 {path}")
        if old == "":
            raise WorkspaceError("要替换的文本不能为空")
        if old == new:
            raise WorkspaceError("新旧文本相同，没有修改")
        text = self.files[path]
        n = text.count(old)
        if n == 0:
            raise WorkspaceError(f"在 {path} 里找不到要替换的文本；先 read_file 看原文再改")
        if n > 1 and not replace_all:
            raise WorkspaceError(f"要替换的文本在 {path} 里出现了 {n} 处；"
                                 "加长上下文使它唯一，或设 replace_all")
        return self._with(path, text.replace(old, new) if replace_all else text.replace(old, new, 1))

    def read(self, path: str, offset: int = 1, limit: int = 200) -> str:
        if path not in self.files:
            raise WorkspaceError(f"没有文件 {path}")
        if offset < 1 or limit < 1:
            raise WorkspaceError("offset 和 limit 必须 >= 1")
        lines = self.files[path].splitlines()
        if lines and offset > len(lines):
            raise WorkspaceError(f"{path} 只有 {len(lines)} 行，offset={offset} 超出范围")
        return "\n".join(f"{n:>6}\t{line[:LINE_CUT]}"
                         for n, line in enumerate(lines[offset - 1: offset - 1 + limit], offset))

    def list(self, pattern: str = "*") -> str:
        names = sorted(n for n in self.files if fnmatch.fnmatchcase(n, pattern))
        return "\n".join(names) if names else EMPTY

    def grep(self, pattern: str, max_hits: int = 50) -> str:
        try:
            rx = regex.compile(pattern)
        except regex.error as e:
            raise WorkspaceError(f"正则不合法：{e}") from None
        hits: list[str] = []
        more = False
        for path in sorted(self.files):
            for n, line in enumerate(self.files[path].splitlines(), 1):
                try:
                    found = rx.search(line, timeout=limits.GREP_TIMEOUT_S)
                except TimeoutError:
                    raise WorkspaceError("这个正则太慢（可能有嵌套的重复），请写得更简单具体") from None
                if found:
                    if len(hits) >= max_hits:
                        more = True
                        break
                    hits.append(f"{path}:{n}: {line[:GREP_LINE_CUT]}")
            if more:
                break
        if not hits:
            return EMPTY
        return "\n".join(hits + (["…还有更多结果，请把正则写得更具体"] if more else []))
