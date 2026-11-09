from evowiki.assistant.tools import build_tools
from evowiki.belief.aspects import AspectKView
from tests.assistant.tools.conftest import T0


def test_six_tools_with_short_specs(store, aspect_k, monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    tools = build_tools(pages=store, kview=AspectKView(aspect_k, T0))
    assert set(tools) == {"search_wiki", "read_page", "read_k", "web_search", "fetch_url",
                          "propose_page_edit"}
    effects = {n: t.spec.effect for n, t in tools.items()}
    assert effects == {"search_wiki": "read", "read_page": "read", "read_k": "read",
                       "web_search": "network", "fetch_url": "network",
                       "propose_page_edit": "propose"}
    for name, t in tools.items():
        assert callable(t.run)                  # contracts.Tool is not runtime_checkable
        assert t.spec.name == name and len(t.spec.description) <= 300
        assert t.spec.parameters["type"] == "object"


def test_web_search_without_key_is_explicit_error(store, aspect_k, ctx, monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    r = build_tools(pages=store, kview=AspectKView(aspect_k, T0))["web_search"].run(
        {"query": "z-score"}, ctx)
    assert r.error and "TAVILY_API_KEY" in r.error
