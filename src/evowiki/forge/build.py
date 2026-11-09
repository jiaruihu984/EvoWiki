"""Build: re-run lint, schema, tests and examples ourselves (DGM/LATM: the coder's claims are never read).

Evaluation cascade: lint -> schema -> tests -> examples -> held-out examples; stops at the first
failing stage. `ws` only needs a `.files` mapping (path -> text). A missing sandbox runtime
propagates.

Held-out examples (LATM: the maker never sees the validation set) are split off the request by
`split_examples`; the coder only ever gets `request_for_coder`, and only this host-side build runs
the held-out ones. Their failures never name or echo which ones failed.

Host calls (plan P2 T5): a tool with `host_tools` runs each example against that example's
cassette (`ToolExample.host`) strictly, VCR "none" record mode: a different, extra or missing host
call fails the example. No live host tool ever runs during a build.
"""
import ast
import hashlib
import json
from dataclasses import dataclass
from typing import Any

import jsonschema

from evowiki.forge import limits
from evowiki.forge.models import BuildReport, RunOutcome, SandboxStats, ToolExample, ToolRequest
from evowiki.forge.sandbox import HostSpec, Sandbox
from evowiki.forge.sandbox.hostcalls import CassetteHost


@dataclass(frozen=True)
class ExampleSplit:
    visible: tuple[ToolExample, ...]
    holdout: tuple[ToolExample, ...]
    request_for_coder: ToolRequest     # the request with `examples=visible, holdout=()`


def _example_key(ex: ToolExample) -> str:
    raw = json.dumps(ex.model_dump(mode="json"), sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def split_examples(request: ToolRequest) -> ExampleSplit:
    """Explicit `request.holdout` wins; otherwise, with at least `HOLDOUT_MIN_EXAMPLES`, hold out
    `max(1, n // HOLDOUT_DIVISOR)` examples: the first k by the sha256 of their JSON, so the split
    is stable across retries and replays and independent of the examples' order."""
    exs = request.examples
    if request.holdout:
        visible, holdout = exs, request.holdout
    elif len(exs) < limits.HOLDOUT_MIN_EXAMPLES:
        visible, holdout = exs, ()
    else:
        k = max(1, len(exs) // limits.HOLDOUT_DIVISOR)
        held = set(sorted(range(len(exs)), key=lambda i: _example_key(exs[i]))[:k])
        visible = tuple(e for i, e in enumerate(exs) if i not in held)
        holdout = tuple(e for i, e in enumerate(exs) if i in held)
    coder = request.model_copy(update={"examples": visible, "holdout": ()})
    return ExampleSplit(visible=visible, holdout=holdout, request_for_coder=coder)


def _schema_problem(raw: str | None) -> str:
    if raw is None:
        return "缺少 schema.json"
    try:
        schema = json.loads(raw)
    except ValueError as e:
        return f"schema.json 不是合法 JSON：{e}"
    if not isinstance(schema, dict) or schema.get("type") != "object":
        return 'schema.json 顶层 type 必须是 "object"'
    if not isinstance(schema.get("description"), str) or not schema["description"].strip():
        return "schema.json 缺少 description"
    try:
        jsonschema.Draft202012Validator.check_schema(schema)
    except jsonschema.SchemaError as e:
        return f"schema.json 不是合法的 JSON Schema：{e.message}"
    return ""


def _run_example(sandbox: Sandbox, files: dict[str, str], request: ToolRequest,
                 ex: ToolExample) -> tuple[RunOutcome, bool]:
    """Run one example; with host tools its cassette is replayed strictly (VCR "none" mode).
    Returns (outcome, replayed): replayed is False when the tool's host calls diverged from the
    cassette or left recorded calls unused. Without host tools no host is offered at all."""
    if not request.host_tools:
        return sandbox.run_tool(files, ex.args), True
    cassette = CassetteHost(ex.host)
    out = sandbox.run_tool(files, ex.args, HostSpec(allowed=request.host_tools, port=cassette))
    mismatch = cassette.mismatched or bool(cassette.unused())
    return out, not mismatch


def build(ws: Any, request: ToolRequest, sandbox: Sandbox) -> BuildReport:
    files: dict[str, str] = dict(ws.files)
    slowest = SandboxStats(wall_s=0.0, exit="ok")
    counts = {"tp": 0, "tf": 0, "ep": 0, "ef": 0, "hp": 0, "hf": 0}
    split = split_examples(request)

    def report(ok: bool, reason: str, *, lint: bool = True, schema: bool = True) -> BuildReport:
        return BuildReport(ok=ok, tests_passed=counts["tp"], tests_failed=counts["tf"],
                           examples_passed=counts["ep"], examples_failed=counts["ef"],
                           holdout_passed=counts["hp"], holdout_failed=counts["hf"],
                           holdout_total=len(split.holdout), lint_ok=lint, schema_ok=schema, sandbox=slowest, reason=reason)

    def see(stats: SandboxStats) -> None:
        nonlocal slowest
        if stats.wall_s >= slowest.wall_s:
            slowest = stats

    # 1. lint
    if "tool.py" not in files:
        return report(False, "缺少 tool.py", lint=False, schema=False)
    for path, src in files.items():
        if path.endswith(".py"):
            try:
                ast.parse(src)
            except SyntaxError as e:
                return report(False, f"{path} 第 {e.lineno} 行语法错误：{e.msg}",
                              lint=False, schema=False)
    # 2. schema
    problem = _schema_problem(files.get("schema.json"))
    if problem:
        return report(False, problem, schema=False)
    validator = jsonschema.Draft202012Validator(json.loads(files["schema.json"]))
    # 3. tests
    if "test_tool.py" not in files:
        return report(False, "缺少 test_tool.py，没有测试不能通过")
    tr = sandbox.run_tests(files)
    see(tr.stats)
    counts["tp"], counts["tf"] = tr.passed, tr.failed
    if tr.error:
        return report(False, f"测试无法运行：{tr.error[:300]}")
    if not tr.cases:
        return report(False, "test_tool.py 里没有任何 test_ 开头的测试")
    if tr.failed:
        names = "、".join(c.name for c in tr.cases if not c.passed)
        return report(False, f"{tr.failed} 个测试失败：{names}")
    # 4. examples (the ones the coder saw)
    first_bad = ""
    for i, ex in enumerate(split.visible, start=1):
        errs = list(validator.iter_errors(ex.args))
        if errs:
            counts["ef"] += 1
            first_bad = first_bad or f"第 {i} 个样例的参数不符合 schema：{errs[0].message}"
            continue
        out, replayed = _run_example(sandbox, files, request, ex)
        see(out.stats)
        if not replayed:
            counts["ef"] += 1
            first_bad = first_bad or f"第 {i} 个样例读取数据的方式和记录不一致"
        elif not out.ok:
            counts["ef"] += 1
            first_bad = first_bad or f"第 {i} 个样例运行出错：{out.error[:200]}"
        elif out.output != ex.expect:
            counts["ef"] += 1
            first_bad = first_bad or f"第 {i} 个样例的输出与期望不一致"
        else:
            counts["ep"] += 1
    if counts["ef"]:
        return report(False, first_bad)
    # 5. held-out examples (never shown to the coder; a failure names no example and no value)
    for ex in split.holdout:
        if next(validator.iter_errors(ex.args), None) is not None:
            counts["hf"] += 1
            continue
        out, replayed = _run_example(sandbox, files, request, ex)
        see(out.stats)
        if replayed and out.ok and out.output == ex.expect:
            counts["hp"] += 1
        else:
            counts["hf"] += 1
    if counts["hf"]:
        return report(False, f"{counts['hf']} 个留出样例没通过（工具可能只记住了给它看的样例）")
    if not split.holdout:
        return report(True, f"测试 {counts['tp']} 个、样例 {counts['ep']} 个全部通过（没有留出样例）")
    return report(True, f"测试 {counts['tp']} 个、样例 {counts['ep']} 个、"
                        f"留出样例 {counts['hp']} 个全部通过")
