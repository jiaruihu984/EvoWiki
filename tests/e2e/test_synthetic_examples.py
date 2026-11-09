"""Public walkthrough fixtures retain the original regression conditions without user text."""

from evowiki.deep.components import validate_config
from evowiki.deep.library_seed import REPO_ROOT, load_component
from evowiki.llm.chat import ChatMessage, ChatRequest
from tests.e2e import materials_backend as materials
from tests.e2e import prefs_backend as prefs
from tests.e2e.wiki_backend import FailClosedScript


def request(text: str, *, done: bool = False) -> ChatRequest:
    messages = [ChatMessage(role="user", text=text)]
    if done:
        messages.append(ChatMessage(role="tool", tool_call_id="p1", text="已执行"))
    return ChatRequest(purpose="deep.write", prompt_version="synthetic@1", subject="public-fixture",
                       system="", messages=tuple(messages))


def test_equipment_fixture_drives_valid_editable_network_with_an_isolated_node():
    draft = materials.practice_turn(request("《节点与边》 目标：解释虚构设备的连接关系"))
    call = draft.tool_calls[0]
    assert call.name == "place_component" and call.args["section_id"] == "n1"
    component = load_component(REPO_ROOT / "src/evowiki/deep/components/network-edit.json")
    config = validate_config(component.config_schema, call.args["config"])
    nodes = {node["id"] for node in config["nodes"]}
    edges = {frozenset((edge["a"], edge["b"])) for edge in config["edges"]}
    assert len(nodes) == 5 and len(edges) == 4
    assert "5" in nodes and not any("5" in edge for edge in edges)
    assert len(edges | {frozenset(("3", "5"))}) == 5
    assert config["editable"] and config["show_sets"] and config["show_counts"]
    assert all("设备" in node["label"] for node in config["nodes"])
    assert all(any(word in body for word in ("虚构", "测试")) for _, _, body in materials.NETWORK_PAGE)
    finished = materials.practice_turn(request("《节点与边》 目标：解释虚构设备的连接关系", done=True))
    assert finished.tool_calls[0].name == "finish"
    assert finished.tool_calls[0].args["interactive_id"] == "network-edit"


def test_synthetic_utterances_still_supply_exact_evidence_to_preference_and_problem_tools():
    script = FailClosedScript()
    preference = prefs.assistant_turn(script, request(prefs.LIKES)).tool_calls[0]
    assert preference.name == "remember_preference"
    assert preference.args["applies"] == "lasting" and preference.args["quote"] == prefs.LIKES
    problem = prefs.assistant_turn(script, request(prefs.COMPLAINT)).tool_calls[0]
    assert problem.name == "report_problem" and problem.args["quote"] == prefs.COMPLAINT
    assert not script.unscripted
