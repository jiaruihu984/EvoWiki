import pytest

from evowiki.assistant.tools.run_python import RunPythonTool
from evowiki.forge.registry import _LazyWasi
from evowiki.forge.sandbox import runtime

pytestmark = pytest.mark.skipif(not runtime.available(), reason="WASI runtime not installed")


@pytest.fixture(scope="module")
def tool():
    return RunPythonTool(_LazyWasi())


def test_prints_come_back(tool):
    r = tool.run({"code": "import io\nprint(io.BytesIO(b'abc').read(2), sum(range(10)))"}, None)
    assert r.error is None and r.content == "运行成功，打印了：\nb'ab' 45\n"


def test_a_failing_snippet_shows_the_traceback_and_what_it_printed(tool):
    r = tool.run({"code": "print('前')\nfrom PIL import Image"}, None)
    assert r.error is None
    assert r.content.startswith("出错前打印了：\n前\n") and "No module named 'PIL'" in r.content


def test_a_runaway_snippet_is_stopped(tool):
    r = tool.run({"code": "while True: pass"}, None)
    assert r.error == "计算量超出上限"
