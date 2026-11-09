"""A layout failure must not masquerade as an empty successful map."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException

from evowiki.nodes import NodeGraph
from evowiki.service import routes_wiki
from evowiki.wikiapp.wiki_schemas import GraphPlacesIn


def test_failed_layout_returns_retryable_error_instead_of_temporary_positions(monkeypatch):
    graph = NodeGraph()
    repo = SimpleNamespace(load=lambda _uid: SimpleNamespace(state=SimpleNamespace(graph=graph)))
    monkeypatch.setattr(routes_wiki, "PgRepo", lambda _pool: repo)
    monkeypatch.setattr(routes_wiki, "_graph", lambda *_args: SimpleNamespace(goals=[], groups=[], nodes=[]))
    layout = Mock(side_effect=RuntimeError("local embedder unavailable"))
    monkeypatch.setattr(routes_wiki, "places_with_blocks", layout)
    svc = SimpleNamespace(pool=None, embedder=None, clock=lambda: None)
    body = GraphPlacesIn(blocks=[{"id": "gradient", "name": "梯度下降", "scope": "学习率、参数更新"}])
    with pytest.raises(HTTPException) as error:
        routes_wiki.post_graph_places(body, svc, "demo")
    assert error.value.status_code == 503
    assert "重试" in error.value.detail
    layout.assert_called_once_with(graph, [], {}, [("gradient", "梯度下降", "学习率、参数更新")], None)
