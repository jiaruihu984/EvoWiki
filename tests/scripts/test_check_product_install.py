"""The install runner's only fake is a real local HTTP model server."""

import json
import threading
from http.server import ThreadingHTTPServer

import pytest
from check_product_install import (
    REPLY,
    LocalModel,
    bootstrap_config,
    isolated_env,
    run,
    verify_identity,
)


@pytest.mark.parametrize("protocol", [1, 2, 4])
def test_install_identity_rejects_incompatible_product_protocol(tmp_path, monkeypatch, protocol):
    monkeypatch.setattr("check_product_install.request", lambda *_: {
        "commit": "release", "instance_root": str(tmp_path),
        "protocol_version": protocol, "drained": False,
    })
    with pytest.raises(AssertionError):
        verify_identity(tmp_path, "http://unused", "release")


def test_install_identity_accepts_current_protocol_and_matching_plugin(tmp_path, monkeypatch):
    monkeypatch.setattr("check_product_install.request", lambda _, path: {
        "commit": "release", "instance_root": str(tmp_path), "protocol_version": 3,
        "drained": False,
    } if path == "/health" else {"ok": True})
    for folder in (tmp_path / "obsidian-plugin",
                   tmp_path / ".evowiki-vault/.obsidian/plugins/evowiki"):
        folder.mkdir(parents=True)
        for name in ("main.js", "manifest.json", "styles.css"):
            (folder / name).write_text("matching release content", encoding="utf-8")
    verify_identity(tmp_path, "http://unused", "release")


def test_local_model_supports_real_anthropic_sdk_json_and_stream():
    anthropic = pytest.importorskip("anthropic")
    server = ThreadingHTTPServer(("127.0.0.1", 0), LocalModel)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    LocalModel.calls = 0
    try:
        with anthropic.Anthropic(api_key="ci-local-model-only", max_retries=0,
                                 base_url=f"http://127.0.0.1:{server.server_port}") as client:
            args = {"model": "claude-haiku-4-5", "max_tokens": 32,
                    "messages": [{"role": "user", "content": "Installation check"}]}
            result = client.messages.create(**args)
            assert result.content[0].text == REPLY and result.stop_reason == "end_turn"
            with client.messages.stream(**args) as stream:
                assert "".join(stream.text_stream) == REPLY
                assert stream.get_final_message().stop_reason == "end_turn"
        assert LocalModel.calls == 2
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)


def test_install_environment_uses_no_caller_keys_or_shared_caches(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-caller-value")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-caller-value")
    monkeypatch.setenv("EVOWIKI_DATABASE_URL", "postgresql://synthetic-caller-db")
    monkeypatch.setenv("EVOWIKI_FORGE", "on")
    monkeypatch.setenv("UV_CACHE_DIR", "shared-cache")
    env = isolated_env(tmp_path)
    assert not any(key in env for key in ("ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY",
                                         "EVOWIKI_DATABASE_URL", "EVOWIKI_FORGE"))
    for key in ("UV_CACHE_DIR", "UV_PYTHON_INSTALL_DIR", "npm_config_cache", "HF_HOME",
                "FASTEMBED_CACHE_PATH", "PLAYWRIGHT_BROWSERS_PATH"):
        assert env[key].startswith(str(tmp_path))


def test_install_runner_refuses_existing_data(tmp_path):
    existing = tmp_path / ".env"
    existing.write_text("synthetic-private-configuration", encoding="utf-8")
    with pytest.raises(RuntimeError, match="fresh checkout|must run on Windows"):
        run(tmp_path, timeout=1)
    assert existing.read_text(encoding="utf-8") == "synthetic-private-configuration"


def test_resume_preserves_original_database_and_rejects_completed_install(tmp_path):
    dest = tmp_path / ".evowiki-run" / "install-check.json"
    dest.parent.mkdir()
    report = {"ok": False, "root": str(tmp_path), "stages": [{"name": "doctor"}, {"name": "up"}]}
    dest.write_text(json.dumps(report), encoding="utf-8")
    config = {"ANTHROPIC_API_KEY": "ci-local-model-only", "DEEPSEEK_API_KEY": "",
              "TAVILY_API_KEY": "", "EVOWIKI_VAULT": (tmp_path / ".evowiki-vault").as_posix(),
              "EVOWIKI_DATABASE_URL": "postgresql://evowiki_app:@127.0.0.1:15432/postgres"}
    original = "".join(f'{key}={json.dumps(value)}\n' for key, value in config.items())
    (tmp_path / ".env").write_text(original, encoding="utf-8")
    assert bootstrap_config(tmp_path) == config
    assert (tmp_path / ".env").read_text(encoding="utf-8") == original
    report["stages"].append({"name": "cpu-embedding"})
    dest.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(RuntimeError, match="before creating its test user"):
        bootstrap_config(tmp_path)


def test_resume_refuses_non_synthetic_credentials(tmp_path):
    dest = tmp_path / ".evowiki-run" / "install-check.json"
    dest.parent.mkdir()
    dest.write_text(json.dumps({"ok": False, "root": str(tmp_path), "stages": []}), encoding="utf-8")
    (tmp_path / ".env").write_text('ANTHROPIC_API_KEY="synthetic-other-configuration"\n', encoding="utf-8")
    with pytest.raises(RuntimeError, match="original synthetic configuration"):
        bootstrap_config(tmp_path)
