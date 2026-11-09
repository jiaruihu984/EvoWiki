"""The coder agent's tools over a `Workspace` (design 2026-10-22 §6.2). Names and semantics follow
Claude Code (Read/Edit/Write/Glob/Grep) and SWE-agent's ACI. The workspace edits are pure functions
of the arguments and the workspace (category "draft"); running anything goes through the `Sandbox`
(category "read"). Failures come back as error results, never exceptions."""

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import jsonschema
from pydantic import BaseModel, ConfigDict, Field

from evowiki.agent.tool_helpers import clip, fail, run_guarded, spec
from evowiki.agent.tools import Tool, ToolContext, ToolResult
from evowiki.forge.sandbox import Sandbox
from evowiki.forge.workspace import Workspace, WorkspaceError

REPORT_CHARS = 4000
OUTPUT_CHARS = 4000
VIEW_CHARS = 9000


@dataclass
class WorkspaceHolder:
    ws: Workspace
    sandbox: Sandbox
    submitted: str | None = None


class _A(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ListArgs(_A):
    pattern: str = Field(default="*", max_length=100, description="文件名通配，如 *.py")


class ReadArgs(_A):
    path: str = Field(max_length=100)
    offset: int = Field(default=1, ge=1, description="从第几行开始（从 1 数）")
    limit: int = Field(default=200, ge=1, le=500, description="最多读几行")


class GrepArgs(_A):
    pattern: str = Field(min_length=1, max_length=200, description="正则表达式")


class EditArgs(_A):
    path: str = Field(max_length=100)
    old_string: str = Field(min_length=1)
    new_string: str
    replace_all: bool = False


class WriteArgs(_A):
    path: str = Field(max_length=100)
    content: str


class RunTestsArgs(_A):
    pass


class TryArgs(_A):
    args: dict[str, Any] = Field(description="传给 run(args) 的参数，必须符合 schema.json")


class SubmitArgs(_A):
    summary: str = Field(min_length=1, max_length=1000, description="一两句话说明做了什么")


def _guard(fn: Callable[[], ToolResult]) -> ToolResult:
    try:
        return fn()
    except WorkspaceError as e:
        return fail(str(e))


class ListFilesTool:
    def __init__(self, holder: WorkspaceHolder):
        self._h = holder
        self.spec = spec("list_files", "列出工作区里的文件，可用通配（如 *.py）。开始工作或不确定有哪些文件时用。",
                         ListArgs, "read", "draft")

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return run_guarded(ListArgs, args, lambda a: ToolResult(content=self._h.ws.list(a.pattern)))


class ReadFileTool:
    def __init__(self, holder: WorkspaceHolder):
        self._h = holder
        self.spec = spec("read_file", "读工作区里的一个文件，带行号（从 1 数）。长文件用 offset 和 limit 分段读；"
                         "改文件前先读。", ReadArgs, "read", "draft")

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        def body(a: ReadArgs) -> ToolResult:
            return _guard(lambda: ToolResult(
                content=clip(self._h.ws.read(a.path, a.offset, a.limit) or "（空文件）", VIEW_CHARS)))
        return run_guarded(ReadArgs, args, body)


class GrepTool:
    def __init__(self, holder: WorkspaceHolder):
        self._h = holder
        self.spec = spec("grep", "用正则在所有文件里搜索，返回 文件:行号: 内容，最多 50 条。找函数或变量在哪用。",
                         GrepArgs, "read", "draft")

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        def body(a: GrepArgs) -> ToolResult:
            return _guard(lambda: ToolResult(content=clip(self._h.ws.grep(a.pattern), VIEW_CHARS)))
        return run_guarded(GrepArgs, args, body)


class EditFileTool:
    def __init__(self, holder: WorkspaceHolder):
        self._h = holder
        self.spec = spec("edit_file", "把文件里的 old_string 换成 new_string。old_string 必须在文件里唯一"
                         "（否则加长上下文，或设 replace_all）。改出语法错误的 .py 会被拒绝，文件保持原样。",
                         EditArgs, "read", "draft")

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        def body(a: EditArgs) -> ToolResult:
            def go() -> ToolResult:
                self._h.ws = self._h.ws.edit(a.path, a.old_string, a.new_string, a.replace_all)
                return ToolResult(content=f"已修改 {a.path}")
            return _guard(go)
        return run_guarded(EditArgs, args, body)


class WriteFileTool:
    def __init__(self, holder: WorkspaceHolder):
        self._h = holder
        self.spec = spec("write_file", "新建文件或整个覆盖。小改动用 edit_file。文件名只能是 xxx.py、"
                         "schema.json、examples.json、README.md。", WriteArgs, "read", "draft")

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        def body(a: WriteArgs) -> ToolResult:
            def go() -> ToolResult:
                self._h.ws = self._h.ws.write(a.path, a.content)
                return ToolResult(content=f"已写入 {a.path}（{len(a.content)} 字符）")
            return _guard(go)
        return run_guarded(WriteArgs, args, body)


class RunTestsTool:
    def __init__(self, holder: WorkspaceHolder):
        self._h = holder
        self.spec = spec("run_tests", "在沙箱里跑 test_tool.py 里的测试，返回通过/失败数和失败详情。",
                         RunTestsArgs, "read", "read")

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        def body(_: RunTestsArgs) -> ToolResult:
            rep = self._h.sandbox.run_tests(self._h.ws.files)
            lines = [f"通过 {rep.passed} / 失败 {rep.failed}"]
            if rep.error:
                lines.append(f"测试没能运行：{rep.error}")
            lines += [f"失败：{c.name}\n{c.detail}" for c in rep.cases if not c.passed]
            return ToolResult(content=clip("\n".join(lines), REPORT_CHARS))
        return run_guarded(RunTestsArgs, args, body)


class TryToolTool:
    def __init__(self, holder: WorkspaceHolder):
        self._h = holder
        self.spec = spec("try_tool", "用一组参数在沙箱里试跑 tool.py 的 run(args)。参数先按 schema.json 校验。",
                         TryArgs, "read", "read")

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        def body(a: TryArgs) -> ToolResult:
            files = self._h.ws.files
            if "schema.json" not in files:
                return fail("还没有 schema.json，先写它再试跑")
            try:
                jsonschema.validate(a.args, json.loads(files["schema.json"]))
            except json.JSONDecodeError as e:
                return fail(f"schema.json 不是合法 JSON：{e.msg}（第 {e.lineno} 行）")
            except jsonschema.ValidationError as e:
                path = "/".join(map(str, e.absolute_path)) or "（顶层）"
                return fail(f"参数不符合 schema.json：{path}：{e.message}")
            except jsonschema.SchemaError as e:
                return fail(f"schema.json 本身不合法：{e.message}")
            out = self._h.sandbox.run_tool(files, a.args)
            if not out.ok:
                return fail(f"运行失败：{out.error}")
            return ToolResult(content=clip(json.dumps(out.output, ensure_ascii=False), OUTPUT_CHARS))
        return run_guarded(TryArgs, args, body)


class SubmitTool:
    def __init__(self, holder: WorkspaceHolder):
        self._h = holder
        self.spec = spec("submit", "做完了就提交当前工作区。提交后不能再改。", SubmitArgs, "read", "draft")

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        def body(a: SubmitArgs) -> ToolResult:
            self._h.submitted = a.summary
            return ToolResult(content="已提交")
        return run_guarded(SubmitArgs, args, body)


def build_tools(holder: WorkspaceHolder) -> list[Tool]:
    return [ListFilesTool(holder), ReadFileTool(holder), GrepTool(holder), EditFileTool(holder),
            WriteFileTool(holder), RunTestsTool(holder), TryToolTool(holder), SubmitTool(holder)]
