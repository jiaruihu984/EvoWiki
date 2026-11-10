"""Local setup uses fake keys/config roots; no service or model requests."""
import json

import evowiki_setup as setup
import pytest


@pytest.fixture
def config(tmp_path):
    root = tmp_path / "学习 checkout"
    root.mkdir()
    (root / ".env").write_text("# 用户设置\nEVOWIKI_RUNTIME_PROVIDER=auto\nCUSTOM='keep # unchanged'\n", encoding="utf-8")
    return root


def status(root):
    return setup.handle({"op": "status"}, root)


def request(root, **changes):
    return {"op": "apply", "provider": "deepseek", "api_key": "fake-test-only",
            "expected_revision": status(root)["revision"], **changes}


def stopped(root, op):
    return {"state": "stopped", "port": 8002}


def test_bootstrap_import_needs_no_database_or_service_dependencies():
    import subprocess
    import sys
    from pathlib import Path

    result = subprocess.run([sys.executable, "-c", """
import importlib.abc
import sys
sys.path.insert(0, sys.argv[1])
class BootstrapOnly(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'psycopg', 'psycopg_pool', 'psutil', 'pydantic', 'fastapi'}:
            raise ModuleNotFoundError('Runtime dependency imported by bootstrap: ' + fullname)
sys.meta_path.insert(0, BootstrapOnly())
from evowiki_setup import parse_config
assert parse_config(b'EVOWIKI_LANGUAGE=zh\\n')['EVOWIKI_LANGUAGE'] == 'zh'
""", str(Path(__file__).resolve().parents[2] / "scripts")],
        capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def test_keyless_status_and_check_are_free_and_secretless(config):
    initial = status(config)
    assert initial["ok"]
    assert not any(p["configured"] for p in initial["providers"])
    checked = setup.handle(request(config, op="check"), config, lambda *_: pytest.fail("no runtime calls"))
    assert checked["ok"]
    assert "fake-test-only" not in json.dumps(checked)
    assert "fake-test-only" not in (config / ".env").read_text(encoding="utf-8")


def test_apply_preserves_unrelated_settings_and_only_writes_main_env(config):
    result = setup.handle(request(config), config, stopped)
    assert result["ok"] and result["restart"]["state"] == "stopped"
    text = (config / ".env").read_text(encoding="utf-8")
    assert "# 用户设置\n" in text and "CUSTOM='keep # unchanged'\n" in text
    assert setup.parse_config(text.encode())["DEEPSEEK_API_KEY"] == "fake-test-only"
    assert "fake-test-only" not in json.dumps(status(config))
    for path in config.rglob("*"):
        if path.is_file() and path.name != ".env":
            assert b"fake-test-only" not in path.read_bytes()


def test_blank_keeps_key_and_explicit_clear_never_switches_provider(config):
    assert setup.handle(request(config), config, stopped)["ok"]
    assert setup.handle(request(config, api_key=""), config, stopped)["ok"]
    assert setup.parse_config((config / ".env").read_bytes())["DEEPSEEK_API_KEY"] == "fake-test-only"
    assert setup.handle(request(config, api_key="", clear_key=True), config, stopped)["ok"]
    result = status(config)
    assert result["selected_provider"] == "deepseek"
    assert not next(p for p in result["providers"] if p["id"] == "deepseek")["configured"]


@pytest.mark.parametrize("changes,code", [
    ({"provider": "unknown"}, "invalid_provider"),
    ({"base_url": "https://user:password@example.com"}, "invalid_url"),
    ({"base_url": "file:///D:/bad"}, "invalid_url"),
    ({"api_key": "fake\nINJECT=x"}, "invalid_value"),
    ({"model": "model-not-in-price-table"}, "missing_price"),
    ({"models": ["new,model"]}, "invalid_value"),
    ({"clear_key": True}, "conflicting_fields"),
    ({"prices_file": "missing-prices.toml"}, "invalid_prices"),
    ({"EVOWIKI_CUSTOM": "x"}, "invalid_request"),
])
def test_invalid_configuration_never_writes_or_restarts(config, changes, code):
    before = (config / ".env").read_bytes()
    result = setup.handle(request(config, **changes), config, lambda *_: pytest.fail("no runtime"))
    assert not result["ok"] and result["code"] == code
    assert (config / ".env").read_bytes() == before
    assert "fake-test-only" not in json.dumps(result)


def test_stale_revision_and_external_edit_during_inspection(config):
    stale = request(config)
    (config / ".env").write_text("CUSTOM=external\n", encoding="utf-8")
    assert setup.handle(stale, config, stopped)["code"] == "revision_conflict"
    current = request(config)
    def edit(root, op):
        (root / ".env").write_text("CUSTOM=newer\n", encoding="utf-8")
        return stopped(root, op)
    assert setup.handle(current, config, edit)["code"] == "revision_conflict"
    assert (config / ".env").read_text() == "CUSTOM=newer\n"


def test_unknown_runtime_refuses_before_save_and_restart_failure_is_truthful(config):
    def refuse(*_):
        raise setup.SetupError("runtime_ownership_or_restart_failed", "restart")
    before = (config / ".env").read_bytes()
    assert setup.handle(request(config), config, refuse)["code"] == "runtime_ownership_or_restart_failed"
    assert (config / ".env").read_bytes() == before
    calls = []
    def failing_restart(root, op):
        calls.append(op)
        if op == "restart":
            raise setup.SetupError("restart_failed", "restart")
        return {"state": "running", "port": 8002}
    result = setup.handle(request(config), config, failing_restart)
    assert calls == ["inspect", "restart"] and not result["ok"]
    assert status(config)["revision"] == result["revision"]
    assert "fake-test-only" in (config / ".env").read_text()


def test_malformed_and_interrupted_config_fail_closed(config):
    (config / ".env").write_text("KEY='unterminated", encoding="utf-8")
    assert status(config)["code"] == "invalid_env"
    run = config / ".evowiki-run"
    run.mkdir()
    (run / "setup-write.json").write_text('{"expected_hash":"x"}', encoding="utf-8")
    assert status(config)["code"] == "interrupted_write"
    with pytest.raises(setup.SetupError, match="interrupted_write"):
        setup.ensure_integrity(config)


def test_exclusive_setup_lock_prevents_overlapping_write_and_recovers_after_kill(config):
    import subprocess
    import sys

    child = subprocess.Popen([sys.executable, "-u", "-c", """
import sys
from pathlib import Path
from evowiki.file_lock import file_lock
with file_lock(Path(sys.argv[1])):
    print('locked', flush=True)
    sys.stdin.read()
""", str(config / ".evowiki-run" / "setup.lock")],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "locked"
        assert setup.handle(request(config), config, stopped)["code"] == "setup_busy"
    finally:
        child.kill()
        child.communicate(timeout=10)
    for _ in range(2):
        assert setup.handle(request(config), config, stopped)["ok"]


def test_dotenv_quotes_and_backslashes_round_trip(config):
    value = r"fake\\quoted'key"
    assert setup.handle(request(config, api_key=value), config, stopped)["ok"]
    assert setup.parse_config((config / ".env").read_bytes())["DEEPSEEK_API_KEY"] == value


def test_native_helper_command_has_fixed_arguments_and_rejects_root():
    from evowiki_args import parse_launcher_args
    assert parse_launcher_args(["setup"], wrapper=True).cmd == "setup"
    with pytest.raises(SystemExit):
        parse_launcher_args(["--root", "D:/other", "setup"], wrapper=True)


def test_global_options_use_existing_env_gates_without_model_selection(config):
    req = {"op": "apply", "expected_revision": status(config)["revision"],
           "forge_enabled": True, "backup_directory": "D:/fake-backups", "backup_keep": 7}
    result = setup.handle(req, config, stopped)
    assert result["ok"]
    env = setup.parse_config((config / ".env").read_bytes())
    assert env["EVOWIKI_FORGE"] == "on" and env["EVOWIKI_BACKUP_KEEP"] == "7"
    assert result["local_options"] == {"forge_enabled": True, "backup_directory": "D:/fake-backups", "backup_keep": 7}
    assert env["EVOWIKI_RUNTIME_PROVIDER"] == "auto"


def test_editing_a_setting_preserves_inline_comment(config):
    (config / ".env").write_text("EVOWIKI_RUNTIME_PROVIDER=auto # 用户说明\n", encoding="utf-8")
    assert setup.handle(request(config), config, stopped)["ok"]
    assert "# 用户说明" in (config / ".env").read_text(encoding="utf-8")


def test_failed_status_still_projects_safe_repair_metadata(config):
    (config / ".env").write_text("OPENAI_API_KEY='qa-secret'\nOPENAI_BASE_URL='broken\nCUSTOM=keep\n", encoding="utf-8")
    result = status(config)
    assert not result["ok"] and result["code"] == "invalid_env"
    assert result["revision"] == setup.revision((config / ".env").read_bytes())
    assert len(result["providers"]) == 7
    assert "qa-secret" not in json.dumps(result)


def test_form_can_repair_a_malformed_owned_assignment_without_discarding_other_lines(config):
    raw = b"# preserve\nOPENAI_API_KEY='qa-secret'\nOPENAI_BASE_URL='broken\nCUSTOM=keep\n"
    (config / ".env").write_bytes(raw)
    body = {"op": "apply", "expected_revision": status(config)["revision"], "provider": "openai",
            "base_url": "http://127.0.0.1:19091/v1", "model": "gpt-5.2", "api_key": ""}
    result = setup.handle(body, config, stopped)
    assert result["ok"]
    parsed = setup.parse_config((config / ".env").read_bytes())
    assert parsed["CUSTOM"] == "keep" and parsed["OPENAI_API_KEY"] == "qa-secret"
    assert "# preserve" in (config / ".env").read_text()


def test_unknown_malformed_assignment_requires_file_repair(config):
    raw = b"CUSTOM='unterminated"
    (config / ".env").write_bytes(raw)
    assert setup.handle(request(config), config, stopped)["code"] == "invalid_env"
    assert (config / ".env").read_bytes() == raw


def test_status_redacts_credentials_embedded_in_nonsecret_configuration(config):
    (config / ".env").write_text("OPENAI_API_KEY=qa-secret\nOPENAI_BASE_URL=https://example.test/qa-secret\n", encoding="utf-8")
    assert "qa-secret" not in json.dumps(status(config))


def test_backup_destination_cannot_write_inside_an_obsidian_vault(tmp_path):
    vault = tmp_path / "notes"
    (vault / ".obsidian").mkdir(parents=True)
    for directory in (vault, vault / "backups"):
        with pytest.raises(setup.SetupError) as exc:
            setup.proposed({"backup_directory": str(directory)}, {}, tmp_path)
        assert exc.value.code == "protected_directory"
    assert setup.proposed({"backup_directory": "backups"}, {}, tmp_path) == {
        "EVOWIKI_BACKUP_DIR": "backups"}

class ModelResponse:
    def __init__(self, page):
        self.payload = json.dumps(page).encode() if not isinstance(page, bytes) else page

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def read1(self, size):
        chunk, self.payload = self.payload[:size], self.payload[size:]
        return chunk


def model_server(monkeypatch, pages):
    calls = []

    class Opener:
        def open(self, req, timeout):
            calls.append((req.full_url, dict(req.header_items()), timeout))
            page = pages.pop(0)
            if isinstance(page, Exception):
                raise page
            return ModelResponse(page)

    monkeypatch.setattr(setup, "build_opener", lambda *_: Opener())
    return calls


def test_models_anthropic_reads_all_pages_without_writes_or_runtime(config, monkeypatch):
    calls = model_server(monkeypatch, [
        {"data": [{"id": "claude-new"}, {"id": "gpt-other"}], "has_more": True, "last_id": "gpt-other"},
        {"data": [{"id": "claude-older"}], "has_more": False},
    ])
    before = (config / ".env").read_bytes()
    result = setup.handle({"op": "models", "provider": "anthropic", "api_key": "fake-only"},
                          config, lambda *_: pytest.fail("metadata cannot restart"))
    assert result["ok"] and result["model_catalog"] == {
        "provider": "anthropic", "models": ["claude-new", "claude-older"]}
    assert calls[0][0] == "https://api.anthropic.com/v1/models?limit=1000"
    assert calls[1][0].endswith("after_id=gpt-other")
    assert calls[0][1]["X-api-key"] == "fake-only"
    assert calls[0][1]["Anthropic-version"] == "2023-06-01"
    assert (config / ".env").read_bytes() == before
    assert sorted(p.name for p in config.iterdir()) == [".env"]
    assert "fake-only" not in json.dumps(result)


@pytest.mark.parametrize("provider,endpoint,model", [
    ("deepseek", "https://api.deepseek.com/models", "deepseek-new"),
    ("openai", "https://api.openai.com/v1/models", "gpt-new"),
    ("gemini", "https://generativelanguage.googleapis.com/v1beta/openai/models", "gemini-new"),
    ("kimi", "https://api.moonshot.ai/v1/models", "kimi-new"),
    ("glm", "https://api.z.ai/api/paas/v4/models", "glm-new"),
])
def test_models_openai_protocol_uses_only_selected_provider_saved_key(config, monkeypatch, provider, endpoint, model):
    spec = setup.PROVIDERS[provider]
    (config / ".env").write_text(f"{spec.key_env}=saved-own-key\nANTHROPIC_API_KEY=other-key\n", encoding="utf-8")
    calls = model_server(monkeypatch, [{"data": [{"id": model}, {"id": "unrelated-model"}]}])
    result = setup.handle({"op": "models", "provider": provider, "api_key": ""}, config)
    assert result["ok"] and calls[0][0] == endpoint
    assert result["model_catalog"]["models"] == [model]
    assert calls[0][1]["Authorization"] == "Bearer saved-own-key"
    assert "other-key" not in str(calls)


def test_custom_models_need_no_selected_model_or_prices(config, monkeypatch):
    calls = model_server(monkeypatch, [{"data": [{"id": "new-model"}]}])
    result = setup.handle({"op": "models", "provider": "openai_compatible", "api_key": "draft-key",
                           "base_url": "http://localhost:1234/v1/"}, config)
    assert result["ok"] and calls[0][0] == "http://localhost:1234/v1/models"


@pytest.mark.parametrize("page", [
    {"data": []}, {"error": "draft-key"}, {"data": [{"id": "draft-key"}]},
    {"data": [{"id": "bad model"}]}, {"data": [{"id": "gpt-m"}], "has_more": True},
    {"data": [{"id": "gpt-m"}], "next_page_token": "cursor"}, b"bad JSON", b"x" * 524289,
], ids=["empty", "upstream-error", "secret", "invalid-id", "unsupported-pagination", "next-page", "bad-json", "oversize"])
def test_invalid_or_unsupported_model_lists_fail_explicitly(config, monkeypatch, page):
    model_server(monkeypatch, [page])
    result = setup.handle({"op": "models", "provider": "openai", "api_key": "draft-key"}, config)
    assert not result["ok"] and "model_catalog" not in result
    assert result["code"] in {"models_invalid_response", "models_unsupported"}
    assert "draft-key" not in json.dumps(result)


def test_models_network_errors_are_safe(config, monkeypatch):
    from urllib.error import HTTPError
    for status, code in [(401, "models_unauthorized"), (404, "models_unsupported"), (500, "models_unavailable")]:
        model_server(monkeypatch, [HTTPError("https://example.com", status, "draft-key", {}, None)])
        result = setup.handle({"op": "models", "provider": "openai", "api_key": "draft-key"}, config)
        assert result["code"] == code and "draft-key" not in json.dumps(result)
    model_server(monkeypatch, [TimeoutError("draft-key")])
    assert setup.handle({"op": "models", "provider": "openai", "api_key": "draft-key"}, config)["code"] == "models_timeout"


def test_models_redirects_never_forward_keys(config):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread

    paths = []

    class Redirect(BaseHTTPRequestHandler):
        def do_GET(self):
            paths.append(self.path)
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/leak")
            self.end_headers()

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = setup.handle({"op": "models", "provider": "openai", "api_key": "fake-only",
                               "base_url": f"http://127.0.0.1:{server.server_port}"}, config)
        assert result["code"] == "models_unavailable" and paths == ["/models"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

@pytest.mark.parametrize("base_url", ["https://user:pass@example.com", "file:///D:/bad", "https://example.com?q=key", "https://example.com/#fragment"])
def test_models_invalid_url_never_opens_network(config, monkeypatch, base_url):
    monkeypatch.setattr(setup, "build_opener", lambda *_: pytest.fail("invalid URL must not reach HTTP"))
    result = setup.handle({"op": "models", "provider": "openai", "api_key": "fake", "base_url": base_url}, config)
    assert result["code"] == "invalid_url"


def test_anthropic_broken_pagination_never_returns_partial_list(config, monkeypatch):
    page = {"data": [{"id": "claude-new"}], "has_more": True, "last_id": "claude-new"}
    model_server(monkeypatch, [page, page])
    result = setup.handle({"op": "models", "provider": "anthropic", "api_key": "fake"}, config)
    assert result["code"] == "models_invalid_response" and "model_catalog" not in result


def test_models_ignore_existing_model_and_prices_errors(config, monkeypatch):
    (config / ".env").write_text("OPENAI_API_KEY=fake\nEVOWIKI_OPENAI_MODEL=unpriced\nEVOWIKI_PRICES_FILE=missing.toml\n", encoding="utf-8")
    model_server(monkeypatch, [{"data": [{"id": "gpt-new"}, {"id": "gpt-image-1"}, {"id": "text-embedding-new"}]}])
    result = setup.handle({"op": "models", "provider": "openai"}, config)
    assert result["ok"] and result["model_catalog"]["models"] == ["gpt-new"]


@pytest.mark.parametrize("stage", ["before_payload", "partial_payload", "after_payload"])
def test_process_exit_during_configuration_write_keeps_integrity_marker(config, stage):
    import subprocess
    import sys

    child = subprocess.run([sys.executable, "-c", """
import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[3])
import evowiki_setup as setup
root, stage = Path(sys.argv[1]), sys.argv[2]
fsync = os.fsync
calls = 0
def interrupt(fd):
    global calls
    fsync(fd)
    calls += 1
    if (calls == 1 and stage == 'before_payload') or (calls == 2 and stage == 'after_payload'):
        os._exit(23)
setup.os.fsync = interrupt
original_open = Path.open
class InterruptedTarget:
    def __init__(self, target):
        self.target = target
    def __getattr__(self, key):
        return getattr(self.target, key)
    def __enter__(self):
        return self
    def __exit__(self, *args):
        self.target.close()
    def write(self, payload):
        self.target.write(payload[:len(payload) // 2])
        self.target.flush()
        fsync(self.target.fileno())
        os._exit(23)
def open_target(path, *args, **kwargs):
    target = original_open(path, *args, **kwargs)
    if stage == 'partial_payload' and path == root / '.env' and args == ('r+b',):
        return InterruptedTarget(target)
    return target
Path.open = open_target
request = dict(op='apply', provider='deepseek', api_key='fake-test-only',
               expected_revision=setup.revision((root / '.env').read_bytes()))
setup.handle(request, root, lambda *_: dict(state='stopped', port=8002))
""", str(config), stage, str(setup.ROOT / "scripts")], capture_output=True, text=True, timeout=10, check=False)
    assert child.returncode == 23, child.stderr
    assert (config / ".evowiki-run" / "setup-write.json").exists()
    assert status(config)["code"] == "interrupted_write"
    assert setup.handle(request(config), config, stopped)["code"] == "interrupted_write"


def test_interrupted_write_discovered_after_inspection_is_not_overwritten(config):
    before = (config / ".env").read_bytes()
    def inspect(root, op):
        run = root / ".evowiki-run"
        run.mkdir()
        (run / "setup-write.json").write_text('{"expected_hash":"incomplete"}', encoding="utf-8")
        return stopped(root, op)
    result = setup.handle(request(config), config, inspect)
    assert result["code"] == "interrupted_write"
    assert (config / ".env").read_bytes() == before
