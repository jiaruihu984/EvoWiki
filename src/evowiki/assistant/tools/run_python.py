"""run_python: run a short standard-library Python snippet in the forge's WASI sandbox (read-only,
no network, no writable files; forge/sandbox/wasi.py) and see what it prints, so code handed to a
beginner is tried on a small sample first (audit 2026-10-27 D11). Third-party libraries are not
there: the model is told so and falls back to the library's documented API."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from evowiki.agent.tool_helpers import clip, fail, run_guarded, spec
from evowiki.agent.tools import ToolContext, ToolResult
from evowiki.forge.registry import _LIMIT_TEXT
from evowiki.forge.sandbox import Sandbox, SandboxUnavailable

OUTPUT_CHARS = 4000

DESCRIPTION = ("在隔离环境里运行一小段 Python 代码，返回它打印的内容或报错。只有标准库（没有 Pillow、"
               "numpy 等第三方库），没有网络，不能写文件；样例数据写在代码里（例如 io.BytesIO）。"
               "要给他能直接运行的代码时，先用它在小样例上试一遍。")

# the guest module: runs the snippet as __main__ and returns what it printed (stdout + stderr)
RUNNER = '''import contextlib
import io
import traceback


def run(args):
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            exec(compile(args["code"], "<代码>", "exec"), {"__name__": "__main__"})
    except BaseException:
        return {"ok": False, "printed": out.getvalue(), "error": traceback.format_exc(limit=-3)}
    return {"ok": True, "printed": out.getvalue()}
'''


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=8000, description="完整的 Python 代码")


class RunPythonTool:
    def __init__(self, sandbox: Sandbox):
        self._sandbox = sandbox
        self.spec = spec("run_python", DESCRIPTION, Args, "read")

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return run_guarded(Args, args, self._run)

    def _run(self, a: Args) -> ToolResult:
        try:
            out = self._sandbox.run_tool({"tool.py": RUNNER}, {"code": a.code})
        except SandboxUnavailable:
            return fail("现在不能运行代码")
        if not out.ok or out.output is None:
            return fail(_LIMIT_TEXT.get(out.stats.exit) or out.error or "运行出错")
        printed = str(out.output.get("printed") or "")
        if not out.output.get("ok"):
            text = f"运行出错：\n{out.output.get('error', '')}"
            if printed:
                text = f"出错前打印了：\n{printed}\n\n{text}"
            return ToolResult(content=clip(text, OUTPUT_CHARS))
        return ToolResult(content=clip(f"运行成功，打印了：\n{printed}" if printed
                                       else "运行成功，没有打印任何内容", OUTPUT_CHARS))
