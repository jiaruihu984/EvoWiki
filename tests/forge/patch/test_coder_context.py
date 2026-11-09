"""离线假模型：预算、诊断和项目地图应在第一次请求时到齐。"""

import pytest

from evowiki.forge import limits
from evowiki.forge.models import Evidence, PatchRequest
from evowiki.forge.patch.coder import run_patch_coder
from evowiki.harness import keys
from evowiki.harness.improver_prompt import SYSTEM
from evowiki.harness.pack import HarnessPack
from evowiki.llm.chat import ChatTurn, ToolCall
from tests.forge.fakes import ScriptedConverse
from tests.forge.patch.test_patch_tools import _holder


def _first(git_repo, tmp_path, *, pack=None, request=None):
    conv = ScriptedConverse(ChatTurn(text="", tool_calls=(
        ToolCall(id="submit", name="submit", args={"summary": "检查完成"}),)))
    run_patch_coder(request or PatchRequest(goal="修复加法", title="修复加法",
                                           failing_case="1+2 应为 3"),
                    conv, holder=_holder(git_repo, tmp_path), trace=None, run_id="",
                    user_id="u1", pack=pack, evidence=Evidence(citations=("trace:r1",)))
    return conv.requests[0]


@pytest.mark.parametrize("cap", [None, 12])
def test_fake_coder_sees_actual_budget(git_repo, tmp_path, cap):
    pack = None if cap is None else HarnessPack(1, {"H4.forge.patch.max_tool_calls": cap})
    first = _first(git_repo, tmp_path, pack=pack)
    assert limits.L2_MAX_TOOL_CALLS == keys.FORGE_PATCH_CAP == 200
    assert f"工具调用最多 {cap or 200} 次" in first.messages[0].text
    assert "没提交的都算白做" not in first.messages[0].text


def test_diagnosis_reaches_fake_coder(git_repo, tmp_path):
    request = PatchRequest(goal="根因：加法误写为减法，只需修正运算符", title="修复加法",
                           failing_case="轨迹 r1：add(1,2) 得到 -1，期望 3",
                           paths_hint=("src/evowiki/deep/mathx.py",))
    first = _first(git_repo, tmp_path, request=request)
    text = first.messages[0].text
    assert all(part in text for part in (request.goal, request.failing_case, request.paths_hint[0]))
    instruction = next(line for line in SYSTEM.splitlines() if "propose_patch" in line)
    assert all(word in instruction for word in ("根因", "goal", "paths_hint", "相关文件"))


def test_fake_coder_starts_with_project_map_and_rules(git_repo, tmp_path):
    text = _first(git_repo, tmp_path).messages[0].text
    for word in ("wikiapp/write_queue.py", "assistant/tools", "bot/actions", "deep/",
                 "store/", "harness/", "产品代码约定", "假", "experiments", "rsib",
                 "失败", "Obsidian"):
        assert word in text
    assert len(text) < 4000
