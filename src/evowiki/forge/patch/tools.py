"""The L2 coder's tools over a `PatchWorkspace` (design §4; plan P3 T6). Names and semantics follow
Claude Code (Read/Edit/Write/Glob/Grep) and SWE-agent's ACI; `outline` is stdlib `ast` (no
tree-sitter dependency). Edits are action steps (idempotent on replay: the same text is written
again); reads and `run_cmd` are read steps (their result is recorded and not re-run).

`run_cmd` runs only the allowlisted argv shapes (`commands.parse`, ruling R4) through the sandbox
`Runner` (Job Object, workspace-write), with the allowlist-built child env (no API keys, no dev
DB). Test counts come from the junit file, never from screen output. Failures come back as error
results, never exceptions."""

import contextlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from evowiki.agent.tool_helpers import clip, fail, run_guarded, spec
from evowiki.agent.tools import Tool, ToolContext, ToolResult
from evowiki.forge import limits
from evowiki.forge.patch import commands, junit
from evowiki.forge.patch.workspace import PatchWorkspace, PatchWorkspaceError
from evowiki.forge.sandbox import SandboxUnavailable
from evowiki.forge.sandbox.jobs import Runner

VIEW_CHARS = 9000
TAIL_CHARS = 4000
PATH_CHARS = 200


@dataclass
class PatchHolder:
    ws: PatchWorkspace
    runner: Runner
    python: str
    test_db_url: str
    tmp: Path
    submitted: str | None = None


class _A(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ListArgs(_A):
    pattern: str = Field(default="**/*.py", min_length=1, max_length=200,
                         description="通配，如 src/evowiki/deep/*.py")


class ReadArgs(_A):
    path: str = Field(min_length=1, max_length=PATH_CHARS)
    offset: int = Field(default=1, ge=1, description="从第几行开始（从 1 数）")
    limit: int = Field(default=200, ge=1, le=500, description="最多读几行")


class GrepArgs(_A):
    pattern: str = Field(min_length=1, max_length=200, description="扩展正则表达式")


class OutlineArgs(_A):
    path: str = Field(min_length=1, max_length=PATH_CHARS)


class EditArgs(_A):
    path: str = Field(min_length=1, max_length=PATH_CHARS)
    old_string: str = Field(min_length=1)
    new_string: str
    replace_all: bool = False


class WriteArgs(_A):
    path: str = Field(min_length=1, max_length=PATH_CHARS)
    content: str


class RunCmdArgs(_A):
    argv: list[str] = Field(min_length=1, max_length=40,
                            description="命令和参数，逐个列出，如 [\"pytest\", \"tests/x/test_a.py\", \"-q\"]")


class SubmitArgs(_A):
    # Metadata only: patch size is bounded by L2_MAX_CHANGED_FILES / L2_MAX_DIFF_LINES.
    # Refusing a completed patch over the length of its explanation wastes the tool budget.
    summary: str = Field(min_length=1, description="一两句话：改了什么、新测试证明了什么")


def _guard(fn: Callable[[], ToolResult]) -> ToolResult:
    try:
        return fn()
    except PatchWorkspaceError as e:
        return fail(str(e))


class _Base:
    def __init__(self, holder: PatchHolder):
        self._h = holder


class ListFilesTool(_Base):
    def __init__(self, holder: PatchHolder):
        super().__init__(holder)
        self.spec = spec("list_files", "按通配列出仓库文件（git 跟踪的文件和你新建的文件），如 src/evowiki/deep/*.py。",
                         ListArgs, "read", "read")

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return run_guarded(ListArgs, args, lambda a: _guard(
            lambda: ToolResult(content=clip(self._h.ws.list(a.pattern), VIEW_CHARS))))


class ReadFileTool(_Base):
    def __init__(self, holder: PatchHolder):
        super().__init__(holder)
        self.spec = spec("read_file", "读仓库里的一个文件，带行号；长文件用 offset/limit 分段读；改文件前先读。",
                         ReadArgs, "read", "read")

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return run_guarded(ReadArgs, args, lambda a: _guard(lambda: ToolResult(
            content=clip(self._h.ws.read(a.path, a.offset, a.limit) or "（空文件）", VIEW_CHARS))))


class GrepTool(_Base):
    def __init__(self, holder: PatchHolder):
        super().__init__(holder)
        self.spec = spec("grep", "在仓库里按正则搜索（git grep），返回 文件:行号: 内容，最多 50 条。",
                         GrepArgs, "read", "read")

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return run_guarded(GrepArgs, args, lambda a: _guard(
            lambda: ToolResult(content=clip(self._h.ws.grep(a.pattern), VIEW_CHARS))))


class OutlineTool(_Base):
    def __init__(self, holder: PatchHolder):
        super().__init__(holder)
        self.spec = spec("outline", "列出一个 .py 文件里的类和函数及行号，先看结构再读细节。",
                         OutlineArgs, "read", "read")

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return run_guarded(OutlineArgs, args, lambda a: _guard(
            lambda: ToolResult(content=clip(self._h.ws.outline(a.path), VIEW_CHARS))))


class EditFileTool(_Base):
    def __init__(self, holder: PatchHolder):
        super().__init__(holder)
        self.spec = spec("edit_file", "把文件里的 old_string 换成 new_string，old_string 必须唯一。只能改允许的目录；"
                         "改出语法错误会被拒绝，文件保持原样。", EditArgs, "read", "action")

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        def body(a: EditArgs) -> ToolResult:
            def go() -> ToolResult:
                self._h.ws.edit(a.path, a.old_string, a.new_string, a.replace_all)
                return ToolResult(content=f"已修改 {a.path}")
            return _guard(go)
        return run_guarded(EditArgs, args, body)


class WriteFileTool(_Base):
    def __init__(self, holder: PatchHolder):
        super().__init__(holder)
        self.spec = spec("write_file", "新建文件或整个覆盖；已有测试不能改，只能在 tests/ 下新建测试文件。",
                         WriteArgs, "read", "action")

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        def body(a: WriteArgs) -> ToolResult:
            def go() -> ToolResult:
                self._h.ws.write(a.path, a.content)
                return ToolResult(content=f"已写入 {a.path}（{len(a.content)} 字符）")
            return _guard(go)
        return run_guarded(WriteArgs, args, body)


class RunCmdTool(_Base):
    def __init__(self, holder: PatchHolder):
        super().__init__(holder)
        self.spec = spec("run_cmd", "运行命令：" + commands.ALLOWED_TEXT, RunCmdArgs, "read", "read")

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return run_guarded(RunCmdArgs, args, self._run)

    def _run(self, a: RunCmdArgs) -> ToolResult:
        if any(len(x) > 300 for x in a.argv):
            return fail("每个参数最多 300 字符")
        h = self._h
        root = Path(h.ws.root)
        try:
            cmd = commands.parse(a.argv, root=root, python=h.python, junit_dir=h.tmp)
        except commands.CmdRefused as e:
            return fail(str(e))
        env = commands.child_env(worktree=root, tmp=h.tmp, test_db_url=h.test_db_url,
                                 python=h.python)
        try:
            res = h.runner.run(cmd.argv, cwd=root, env=env, wall_s=limits.L2_CMD_WALL_S)
        except SandboxUnavailable:
            return fail("沙箱不可用，命令没有运行")
        tail = res.output[-TAIL_CHARS:]
        if cmd.kind != "pytest":
            head = f"退出码 {res.exit_code}" if res.exit_code is not None else "命令被终止"
            return ToolResult(content=f"{head}（{res.stats.exit}）\n{tail}")
        assert cmd.junit is not None
        try:
            got = junit.read(cmd.junit)
        except junit.JunitError:
            return ToolResult(content=f"测试没能运行（{res.stats.exit}）\n{tail}")
        finally:
            with contextlib.suppress(OSError):
                cmd.junit.unlink(missing_ok=True)
        counts = {k: sum(1 for v in got.values() if v == k)
                  for k in ("passed", "failed", "error", "skipped")}
        line = f"通过 {counts['passed']} / 失败 {counts['failed']} / 出错 {counts['error']}"
        if counts["skipped"]:
            line += f" / 跳过 {counts['skipped']}"
        bad = [n for n, v in got.items() if v in ("failed", "error")][:20]
        parts = [line] + ([("没通过：" + "，".join(bad))] if bad else []) + [tail]
        return ToolResult(content="\n".join(parts))


class SubmitTool(_Base):
    def __init__(self, holder: PatchHolder):
        super().__init__(holder)
        self.spec = spec("submit", "做完了就提交：一两句话说明改了什么、新测试证明了什么。",
                         SubmitArgs, "read", "draft")

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        def body(a: SubmitArgs) -> ToolResult:
            self._h.submitted = a.summary
            return ToolResult(content="已提交")
        return run_guarded(SubmitArgs, args, body)


def build_tools(holder: PatchHolder) -> list[Tool]:
    return [ListFilesTool(holder), ReadFileTool(holder), GrepTool(holder), OutlineTool(holder),
            EditFileTool(holder), WriteFileTool(holder), RunCmdTool(holder), SubmitTool(holder)]
