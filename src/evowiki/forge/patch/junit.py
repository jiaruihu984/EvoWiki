"""Test outcomes from pytest's `--junitxml` file (SWE-bench harness; plan P3 T4): results come from
this file, never from screen output or the model's word. Fail closed: a missing, oversized,
malformed or DTD-bearing file raises JunitError (no entity tricks, no partial pass)."""

from pathlib import Path, PurePosixPath
from typing import Literal
from xml.etree import ElementTree

from evowiki.forge import limits

Outcome = Literal["passed", "failed", "error", "skipped"]


class JunitError(ValueError):
    """The junit file cannot be trusted as a test result."""


def _nodeid(classname: str, name: str, file: str | None) -> str:
    parts = [p for p in classname.split(".") if p]
    if file:
        f = PurePosixPath(file.replace("\\", "/"))
        module = list(f.with_suffix("").parts)
        classes = parts[len(module):] if parts[:len(module)] == module else []
        return "::".join([f.as_posix(), *classes, name])
    first_class = next((i for i, p in enumerate(parts) if p[:1].isupper()), len(parts))
    module, classes = parts[:first_class], parts[first_class:]
    if not module:
        raise JunitError("测试结果文件格式不对")
    return "::".join(["/".join(module) + ".py", *classes, name])


def read(path: Path) -> dict[str, Outcome]:
    """nodeid ("tests/x/test_a.py::test_b", "tests/x/test_a.py::TestK::test_b") -> outcome."""
    try:
        raw = path.read_bytes()
    except OSError as e:
        raise JunitError(f"没有测试结果文件：{e}") from None
    if len(raw) > limits.JUNIT_MAX_BYTES:
        raise JunitError("测试结果文件太大")
    upper = raw.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise JunitError("测试结果文件格式不对")
    try:
        root = ElementTree.fromstring(raw)
    except ElementTree.ParseError:
        raise JunitError("测试结果文件格式不对") from None
    if root.tag not in ("testsuites", "testsuite"):
        raise JunitError("测试结果文件格式不对")
    out: dict[str, Outcome] = {}
    for case in root.iter("testcase"):
        name = case.get("name", "")
        if not name:
            raise JunitError("测试结果文件格式不对")
        outcome: Outcome = ("failed" if case.find("failure") is not None else
                            "error" if case.find("error") is not None else
                            "skipped" if case.find("skipped") is not None else "passed")
        nodeid = _nodeid(case.get("classname", ""), name, case.get("file"))
        # a test reported twice (e.g. a teardown error after a pass) keeps the worse outcome
        if out.get(nodeid) not in (None, "passed", "skipped") and outcome in ("passed", "skipped"):
            continue
        out[nodeid] = outcome
    return out
