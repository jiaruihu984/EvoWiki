import json

from evowiki.pipeline.workspace import Workspace


def test_workspace_reads_connection_requests(tmp_path):
    ws = Workspace(tmp_path)
    assert ws.connection_request_paths() == []
    lines = [{"path": ["pca", "svd"], "requested_for": "a|b"},
             {"path": ["eigenvalue", "pca"], "requested_for": "c"},
             {"path": ["pca", "svd"], "requested_for": "dup"}]
    (tmp_path / "connection_requests.jsonl").write_text(
        "".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
    assert ws.connection_request_paths() == [["pca", "svd"], ["eigenvalue", "pca"]]
