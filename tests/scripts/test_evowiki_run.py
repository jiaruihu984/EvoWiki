import subprocess
import sys
from pathlib import Path

import pytest
from evowiki_run import diagnose, initialize, model_configuration_error, runtime_args


@pytest.mark.parametrize(("device", "extra"), [("cpu", "embed"), ("cuda", "embed-gpu")])
def test_selects_exactly_one_embedding_extra(device, extra):
    args = runtime_args({"EVOWIKI_EMBED_PROVIDERS": device})
    assert args == ["--locked", "--exact", "--extra", "llm", "--extra", extra, "--extra", "deep"]


def test_unknown_device_is_rejected():
    with pytest.raises(ValueError, match="cpu 或 cuda"):
        runtime_args({"EVOWIKI_EMBED_PROVIDERS": "cdua"})


def test_missing_runtime_repairs_the_selected_wheel(tmp_path):
    args = runtime_args({"EVOWIKI_EMBED_PROVIDERS": "cuda"}, tmp_path)
    assert args[-2:] == ["--reinstall-package", "onnxruntime-gpu"]


def test_healthy_runtime_is_not_reinstalled(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "is_file", lambda _: True)
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 0))
    assert "--reinstall-package" not in runtime_args({"EVOWIKI_EMBED_PROVIDERS": "cuda"}, tmp_path)


def test_cpu_switch_repairs_shared_files_even_if_gpu_wheel_was_overwritten(tmp_path, monkeypatch):
    # A CPU module can sit on top of GPU package metadata. Exact sync removes the
    # GPU wheel (and their shared files), so the CPU wheel must be installed last.
    import os

    (tmp_path / "onnxruntime.py").write_text(
        "def get_available_providers(): return ['CPUExecutionProvider']\n", encoding="utf-8")
    metadata = tmp_path / "onnxruntime_gpu-1.0.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text("Name: onnxruntime-gpu\nVersion: 1.0\n", encoding="utf-8")
    run = subprocess.run
    monkeypatch.setattr(Path, "is_file", lambda _: True)
    monkeypatch.setattr(subprocess, "run", lambda args, **kw: run(
        [sys.executable, *args[1:]], env={**os.environ, "PYTHONPATH": str(tmp_path)}, **kw))
    args = runtime_args({"EVOWIKI_EMBED_PROVIDERS": "cpu"}, tmp_path)
    assert args[-2:] == ["--reinstall-package", "onnxruntime"]


def test_initialize_preserves_existing_config_and_vault(tmp_path):
    import json

    (tmp_path / ".env.example").write_text("# 中文配置\nDEEPSEEK_API_KEY=\n", encoding="utf-8")
    assert initialize(tmp_path) is True
    assert "中文配置" in (tmp_path / ".env").read_text(encoding="utf-8")
    plugins = tmp_path / ".evowiki-vault" / ".obsidian" / "community-plugins.json"
    assert json.loads(plugins.read_text(encoding="utf-8")) == ["evowiki"]
    (tmp_path / ".env").write_text("DEEPSEEK_API_KEY=keep-private\n", encoding="utf-8")
    plugins.write_text('["another-plugin"]', encoding="utf-8")
    assert initialize(tmp_path) is False
    assert "keep-private" in (tmp_path / ".env").read_text(encoding="utf-8")
    assert json.loads(plugins.read_text(encoding="utf-8")) == ["another-plugin"]


def test_doctor_never_prints_secret_values(tmp_path, capsys):
    assert diagnose(tmp_path, {"DEEPSEEK_API_KEY": "SECRET-MUST-NOT-APPEAR", "TAVILY_API_KEY": "HIDDEN"}) == 1
    text = capsys.readouterr().out
    assert "DeepSeek：已配置" in text and "联网搜索：已配置" in text
    assert "SECRET" not in text and "HIDDEN" not in text


@pytest.mark.parametrize("model_env", [
    {"DEEPSEEK_API_KEY": "not-contacted"},
    {"OPENAI_API_KEY": "not-contacted"},
    {"GEMINI_API_KEY": "not-contacted"},
    {"MOONSHOT_API_KEY": "not-contacted"},
    {"ZAI_API_KEY": "not-contacted"},
    {"EVOWIKI_COMPATIBLE_API_KEY": "not-contacted",
     "EVOWIKI_COMPATIBLE_BASE_URL": "https://example.invalid/v1",
     "EVOWIKI_COMPATIBLE_MODEL": "test-model"},
])
def test_doctor_accepts_required_configuration_without_paid_validation(tmp_path, monkeypatch, capsys, model_env):
    import evowiki_run as runner

    (tmp_path / ".env.example").write_text("DEEPSEEK_API_KEY=\n", encoding="utf-8")
    initialize(tmp_path)
    monkeypatch.setattr(runner.sys, "platform", "win32")
    monkeypatch.setattr(runner, "git_runtime_dir", lambda: tmp_path / "Git runtime")
    monkeypatch.setattr(runner, "missing_vc_runtime", list)
    monkeypatch.setattr(runner.shutil, "which", lambda name: name)
    monkeypatch.setattr(runner.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(
        cmd, 0, "--locked --exact --isolated --no-project" if cmd[1:] == ["run", "--help"] else "v22.20.0\n", ""))
    class Socket:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def settimeout(self, value):
            pass
        def connect_ex(self, address):
            return 1
    monkeypatch.setattr(runner.socket, "socket", Socket)
    assert diagnose(tmp_path, {**model_env, "EVOWIKI_DATABASE_URL": "postgresql://app@127.0.0.1:19999/wiki"}) == 0
    assert "密钥有效性" in capsys.readouterr().out
    monkeypatch.setattr(runner, "git_runtime_dir", lambda: None)
    assert diagnose(tmp_path, {**model_env, "EVOWIKI_DATABASE_URL": "postgresql://app@127.0.0.1:19999/wiki"}) == 1
    assert "完整官方 Git for Windows" in capsys.readouterr().out
    monkeypatch.setattr(runner, "git_runtime_dir", lambda: tmp_path / "Git runtime")
    monkeypatch.setattr(runner, "missing_vc_runtime", lambda: ["msvcp140_1.dll"])
    assert diagnose(tmp_path, {**model_env, "EVOWIKI_DATABASE_URL": "postgresql://app@127.0.0.1:19999/wiki"}) == 1
    output = capsys.readouterr().out
    assert "msvcp140_1.dll" in output and "https://aka.ms/vc14/vc_redist.x64.exe" in output
    assert "管理员" in output


def test_vc_runtime_checks_system_libraries_without_borrowing_app_dlls(tmp_path, monkeypatch):
    import ctypes

    import evowiki_run as runner

    monkeypatch.setattr(runner.sys, "platform", "win32")
    monkeypatch.setenv("SystemRoot", str(tmp_path))
    loaded = []
    def load(path):
        loaded.append(Path(path))
        if Path(path).name == "msvcp140_1.dll":
            raise OSError("not installed")
    monkeypatch.setattr(ctypes, "WinDLL", load, raising=False)
    assert runner.missing_vc_runtime() == ["msvcp140_1.dll"]
    assert len(loaded) == 4 and all(path.parent == tmp_path / "System32" for path in loaded)


def test_wrapper_stops_old_runtime_before_locked_sync(tmp_path, monkeypatch):
    import os

    import evowiki_run as runner

    root = tmp_path / "checkout 中文 space"
    (root / "scripts").mkdir(parents=True)
    (root / ".venv" / "Scripts").mkdir(parents=True)
    (root / ".venv" / "Scripts" / "python.exe").touch()
    (root / ".env").write_text("DEEPSEEK_API_KEY=fake\nEVOWIKI_DATABASE_URL=postgresql://app@127.0.0.1:19999/wiki\n", encoding="utf-8")
    monkeypatch.setattr(runner.subprocess, "check_output", lambda *a, **kw: str(root / ".git"))
    monkeypatch.setattr(runner.shutil, "which", lambda name: name)
    monkeypatch.setattr(sys, "argv", ["evowiki_run.py", "up"])
    calls = []
    def call(cmd):
        calls.append(cmd)
        return 75 if "preflight" in cmd else 0
    monkeypatch.setattr(runner.subprocess, "call", call)
    monkeypatch.setattr(runner, "runtime_args", lambda *a: ["--locked"])
    monkeypatch.setattr(runner, "missing_vc_runtime", list)
    if os.name != "nt":
        pytest.skip("Windows launcher bootstrap")
    assert runner.main() == 0
    assert "preflight" in calls[0] and "--locked" not in calls[0]
    assert "--locked" in calls[1] and "up" in calls[1]


def test_clean_checkout_init_works_outside_original_location(tmp_path):
    import json
    import os
    import shutil

    root = tmp_path / "new checkout 学习"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    source = Path(__file__).resolve().parents[2]
    for name in ("evowiki_run.py", "evowiki_args.py", "envfile.py", "git_runtime.py", "launcher_i18n.py", "launcher.en.json"):
        shutil.copy2(source / "scripts" / name, scripts / name)
    shutil.copy2(source / ".env.example", root / ".env.example")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("EVOWIKI_") and not k.endswith("_API_KEY")}
    env["PYTHONIOENCODING"] = "utf-8"
    result = subprocess.run([sys.executable, str(scripts / "evowiki_run.py"), "init"], cwd=tmp_path,
                            env=env, capture_output=True, text=True, encoding="utf-8", check=False)
    assert result.returncode == 0, result.stderr
    assert "EVOWIKI_DATABASE_URL=postgresql://" in (root / ".env").read_text(encoding="utf-8")
    assert json.loads((root / ".evowiki-vault/.obsidian/community-plugins.json").read_text(encoding="utf-8")) == ["evowiki"]
    from envfile import read_env_file
    from evowiki_run import initial_setup_needed
    assert initial_setup_needed(read_env_file(root / ".env"))


@pytest.mark.parametrize(("args", "stops"), [
    (["restore", "--backup=x", "--database=new", "--target-dir=D:/restore", "--confirm"], True),
    (["restore", "--backup", "x", "--database", "new", "--target-dir", "D:/restore"], False),
])
def test_restore_only_stops_for_explicit_valid_command_shape(tmp_path, monkeypatch, args, stops):
    import evowiki_run as runner

    monkeypatch.setattr(runner.subprocess, "check_output", lambda *a, **kw: str(tmp_path / ".git"))
    monkeypatch.setattr(runner.shutil, "which", lambda name: name)
    monkeypatch.setattr(sys, "argv", ["evowiki_run.py", *args])
    calls = []
    monkeypatch.setattr(runner.subprocess, "call", lambda cmd: calls.append(cmd) or 0)
    assert runner.main() == 0
    assert any("down" in cmd for cmd in calls) is stops
    assert "evowiki_backup.py" in calls[-1][calls[-1].index("python") + 1]


@pytest.mark.parametrize("args", [
    ["up", "--help"], ["up", "-h"], ["down", "--help"],
    ["uninstall", "--help"], ["restore", "--help"], ["backups", "--help"],
    ["init", "--help"], ["doctor", "--help"], ["--help"], [],
])
def test_help_with_running_stale_instance_never_preflights_or_syncs(tmp_path, monkeypatch, args):
    import json

    import evowiki_run as runner
    import psutil

    root = tmp_path / "running 中文 checkout"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    for name in ("evowiki_run.py", "evowiki_ctl.py", "evowiki_backup.py"):
        (scripts / name).touch()
    python = root / ".venv" / ("Scripts/python.exe" if runner.os.name == "nt" else "bin/python")
    python.parent.mkdir(parents=True)
    python.touch()
    source = root / "src" / "evowiki" / "changed.py"
    source.parent.mkdir(parents=True)
    source.write_text("old source", encoding="utf-8")
    server_code = ("from http.server import HTTPServer,BaseHTTPRequestHandler; "
                   "s=HTTPServer(('127.0.0.1',0),BaseHTTPRequestHandler); "
                   "print(s.server_port,flush=True); s.serve_forever()")
    backend = subprocess.Popen([sys.executable, "-c", server_code], cwd=root,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    try:
        port = int(backend.stdout.readline())
        state_file = root / ".evowiki-run" / "state.json"
        state_file.parent.mkdir()
        state_file.write_text(json.dumps({"backend": {"pid": backend.pid,
            "create_time": psutil.Process(backend.pid).create_time(), "root": str(root),
            "port": port, "signature": "before-source-change"}}), encoding="utf-8")
        source.write_text("changed source", encoding="utf-8")
        before = state_file.read_bytes()
        def forbidden(*a, **kw):
            pytest.fail("help must not initialize, inspect config, preflight, probe tools or sync")
        for name in ("initialize", "diagnose", "read_env_file", "git_runtime_dir", "missing_vc_runtime", "runtime_args"):
            monkeypatch.setattr(runner, name, forbidden)
        monkeypatch.setattr(runner.subprocess, "check_output", forbidden)
        monkeypatch.setattr(runner.shutil, "which", forbidden)
        monkeypatch.setattr(runner, "__file__", str(scripts / "evowiki_run.py"))
        monkeypatch.setattr(sys, "argv", ["evowiki_run.py", *args])
        calls = []
        monkeypatch.setattr(runner.subprocess, "call", lambda cmd: calls.append(cmd) or 0)
        assert runner.main() == 0
        assert backend.poll() is None and state_file.read_bytes() == before
        assert not (root / ".env").exists() and not (root / ".evowiki-vault").exists()
        assert calls == []
    finally:
        backend.terminate()
        backend.wait(timeout=10)
        backend.stdout.close()


@pytest.mark.parametrize(("args", "code"), [
    (["up", "--help"], 0), (["init", "-h"], 0), (["restore", "--help"], 0),
    (["--help"], 0), ([], 0), (["up", "--bogus"], 2), (["init", "--bogus"], 2),
])
def test_fresh_checkout_help_or_invalid_args_needs_no_project_dependencies_or_new_files(tmp_path, args, code):
    import shutil

    root = tmp_path / "fresh 中文 checkout"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    source = Path(__file__).resolve().parents[2]
    for name in ("evowiki_run.py", "evowiki_args.py", "launcher_i18n.py", "launcher.en.json"):
        shutil.copy2(source / "scripts" / name, scripts / name)
    (root / "README.md").write_text("Installation instructions", encoding="utf-8")
    before = {p.relative_to(root) for p in root.rglob("*")}
    # Only the shared stdlib grammar is present; dotenv/configuration modules are absent.
    result = subprocess.run([sys.executable, "-I", "-X", "utf8", str(scripts / "evowiki_run.py"), *args],
                            cwd=tmp_path, capture_output=True, text=True, encoding="utf-8", check=False)
    assert result.returncode == code, result.stderr
    if code == 0:
        assert "README.md" in result.stdout
        assert (args[0] if args and not args[0].startswith("-") else "up") in result.stdout
    else:
        assert "unrecognized arguments" in result.stderr
    assert {p.relative_to(root) for p in root.rglob("*")} == before


@pytest.mark.parametrize("args", [
    ["up", "--bogus"], ["up", "--port", "invalid"], ["up", "--port", "65536"],
    ["up", "--resume"], ["up", "--from", "unknown"],
    ["--root", "up", "up", "--from", "live"],
    ["init", "--bogus"], ["doctor", "--bogus"], ["backup", "--bogus"],
    ["restore", "--confirm", "--backup", "x", "--database", "new", "--target-dir", "target", "--bogus"],
])
def test_invalid_arguments_are_rejected_before_configuration_or_lifecycle(tmp_path, monkeypatch, args):
    import evowiki_run as runner

    script = tmp_path / "scripts" / "evowiki_run.py"
    script.parent.mkdir()
    state = tmp_path / ".evowiki-run" / "state.json"
    state.parent.mkdir()
    state.write_text('{"backend":{"pid":123,"signature":"old"}}', encoding="utf-8")
    before = state.read_bytes()
    def forbidden(*a, **kw):
        pytest.fail("invalid arguments must not inspect config, initialize, preflight or sync")
    for name in ("initialize", "diagnose", "read_env_file", "git_runtime_dir", "missing_vc_runtime", "runtime_args"):
        monkeypatch.setattr(runner, name, forbidden)
    for name in ("call", "run", "check_output"):
        monkeypatch.setattr(runner.subprocess, name, forbidden)
    monkeypatch.setattr(runner, "__file__", str(script))
    monkeypatch.setattr(sys, "argv", ["evowiki_run.py", *args])
    with pytest.raises(SystemExit) as info:
        runner.main()
    assert info.value.code == 2 and state.read_bytes() == before
    assert not (tmp_path / ".env").exists() and not (tmp_path / ".evowiki-vault").exists()


def test_up_as_a_vault_value_does_not_route_status_to_startup(tmp_path, monkeypatch):
    import evowiki_run as runner

    monkeypatch.setattr(runner.subprocess, "check_output", lambda *a, **kw: str(tmp_path / ".git"))
    monkeypatch.setattr(runner.shutil, "which", lambda name: name)
    monkeypatch.setattr(runner, "initialize", lambda *a: pytest.fail("status must not initialize"))
    monkeypatch.setattr(runner, "runtime_args", lambda *a: pytest.fail("status must not sync"))
    monkeypatch.setattr(sys, "argv", ["evowiki_run.py", "--root", "up", "status", "--vault", "up"])
    calls = []
    monkeypatch.setattr(runner.subprocess, "call", lambda cmd: calls.append(cmd) or 0)
    assert runner.main() == 0
    assert len(calls) == 1 and "--no-sync" in calls[0]
    assert calls[0][-5:] == ["--root", "up", "status", "--vault", "up"]


def test_preflight_keeps_a_vault_named_up_instead_of_replacing_its_value(tmp_path, monkeypatch):
    import evowiki_run as runner

    (tmp_path / ".env").write_text("DEEPSEEK_API_KEY=fake\nEVOWIKI_DATABASE_URL=postgresql://app@127.0.0.1:19999/wiki\n", encoding="utf-8")
    python = tmp_path / ".venv" / ("Scripts/python.exe" if runner.os.name == "nt" else "bin/python")
    python.parent.mkdir(parents=True)
    python.touch()
    monkeypatch.setattr(runner.subprocess, "check_output", lambda *a, **kw: str(tmp_path / ".git"))
    monkeypatch.setattr(runner.shutil, "which", lambda name: name)
    monkeypatch.setattr(runner, "git_runtime_dir", lambda: tmp_path)
    monkeypatch.setattr(runner, "missing_vc_runtime", list)
    monkeypatch.setattr(runner, "runtime_args", lambda *a: pytest.fail("matching preflight must not sync"))
    monkeypatch.setattr(sys, "argv", ["evowiki_run.py", "up", "--vault", "up"])
    calls = []
    monkeypatch.setattr(runner.subprocess, "call", lambda cmd: calls.append(cmd) or 0)
    if runner.os.name != "nt":
        pytest.skip("Windows launcher bootstrap")
    assert runner.main() == 0
    assert calls[0][2] == "preflight"
    assert calls[0][-2:] == ["--vault", "up"]
    assert "--no-sync" in calls[1]


@pytest.mark.parametrize("env,expected", [
    ({"EVOWIKI_RUNTIME_PROVIDER": "openai", "GEMINI_API_KEY": "fake"}, "尚未配置模型密钥"),
    ({"EVOWIKI_RUNTIME_PROVIDER": "unsupported", "OPENAI_API_KEY": "fake"}, "EVOWIKI_RUNTIME_PROVIDER"),
    ({"EVOWIKI_RUNTIME_PROVIDER": "openai_compatible", "EVOWIKI_COMPATIBLE_API_KEY": "fake"}, "EVOWIKI_COMPATIBLE_BASE_URL"),
    ({"EVOWIKI_COMPATIBLE_API_KEY": "fake", "EVOWIKI_COMPATIBLE_BASE_URL": "https://example.invalid/v1"}, "EVOWIKI_COMPATIBLE_MODEL"),
    ({"OPENAI_API_KEY": "  "}, "尚未配置模型密钥"),
])
def test_model_configuration_requires_the_selected_provider(env, expected):
    assert expected in model_configuration_error(env)


def test_initialize_refuses_foreign_managed_vault_before_modifying_it(tmp_path):
    import json

    (tmp_path / ".env").write_text("EXISTING=1", encoding="utf-8")
    vault = tmp_path / ".evowiki-vault"
    vault.mkdir()
    (vault / ".evowiki-owner.json").write_text(
        json.dumps({"instance_root": str(tmp_path / "other")}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="归属"):
        initialize(tmp_path)
    assert not (vault / ".obsidian").exists()
