from types import SimpleNamespace

from fastapi.testclient import TestClient

from evowiki.service import capabilities as module
from evowiki.service.app import create_app


def test_optional_features_are_reported_without_leaking_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "wasi_available", lambda: False)
    svc = SimpleNamespace(gateway=object(), deep=SimpleNamespace(llm=object()),
                          library=SimpleNamespace(list=lambda: [object()]))
    report = module.capabilities(svc, {"DEEPSEEK_API_KEY": "NEVER-PRINT-THIS"})
    features = {row["name"]: row for row in report["features"]}
    assert features["学习规划与助手"]["configured"] is True
    assert features["页面生成"]["configured"] is True
    assert features["学习规划与助手"]["detail"] == "已配置"
    assert features["页面生成"]["detail"] == "已配置"
    assert features["预置交互组件"]["detail"] == "已就绪"
    assert features["联网搜索"]["configured"] is False
    assert features["扫描页识别"]["configured"] is False
    assert features["新工具运行环境"]["configured"] is False
    assert report["model_access_checked"] is False and "NEVER-PRINT-THIS" not in str(report)
    python = tmp_path / "layout-python.exe"
    python.touch()
    report = module.capabilities(svc, {"EVOWIKI_LAYOUT_PYTHON": str(python), "TAVILY_API_KEY": "SECRET"})
    assert all(row["configured"] for row in report["features"] if row["name"] in ("扫描页识别", "联网搜索"))
    assert all(row["detail"] == "已配置" for row in report["features"] if row["name"] in ("扫描页识别", "联网搜索"))


def test_capabilities_route_handles_an_unconfigured_service(monkeypatch):
    monkeypatch.setattr(module, "wasi_available", lambda: False)
    for key in ("ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY", "TAVILY_API_KEY", "EVOWIKI_LAYOUT_PYTHON"):
        monkeypatch.delenv(key, raising=False)
    response = TestClient(create_app(SimpleNamespace(durable=None)), base_url="http://127.0.0.1").get("/health/capabilities")
    assert response.status_code == 200
    assert all(not row["configured"] for row in response.json()["features"])


def test_capabilities_api_distinguishes_configuration_from_local_resources_without_probes(monkeypatch):
    def no_probe(*args, **kwargs):
        raise AssertionError("capability reporting must not make provider calls")
    gateway = SimpleNamespace(call=no_probe, converse=no_probe)
    svc = SimpleNamespace(durable=None, gateway=gateway, deep=SimpleNamespace(llm=gateway),
                          library=SimpleNamespace(list=lambda: [object()]))
    monkeypatch.setattr(module, "wasi_available", lambda: True)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "new-unverified-key")
    monkeypatch.setenv("TAVILY_API_KEY", "new-unverified-search-key")
    response = TestClient(create_app(svc), base_url="http://127.0.0.1").get("/health/capabilities")
    assert response.status_code == 200
    report = response.json()
    rows = {row["name"]: row for row in report["features"]}
    for name in ("学习规划与助手", "页面生成", "联网搜索", "扫描页识别"):
        assert rows[name]["configured"] and rows[name]["detail"] == "已配置"
    assert rows["预置交互组件"]["detail"] == rows["新工具运行环境"]["detail"] == "已就绪"
    assert not report["model_access_checked"] and "new-unverified" not in response.text


def test_missing_capabilities_give_the_actual_configuration_or_preparation_step(monkeypatch):
    monkeypatch.setattr(module, "wasi_available", lambda: False)
    report = module.capabilities(SimpleNamespace(), {})
    rows = {row["name"]: row for row in report["features"]}
    assert "ANTHROPIC_API_KEY" in rows["学习规划与助手"]["detail"]
    assert "后端日志" in rows["页面生成"]["detail"]
    assert "--env-file .env" in rows["预置交互组件"]["detail"]
    assert "TAVILY_API_KEY" in rows["联网搜索"]["detail"]
    assert "EVOWIKI_LAYOUT_PYTHON" in rows["扫描页识别"]["detail"]
