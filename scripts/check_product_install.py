"""Exercise the Windows product launcher in a fresh checkout, with a local model endpoint.

This intentionally downloads the normal locked runtime and Chromium. It never imports test
fixtures, borrows another environment/database, or contacts a paid model provider.
"""

import argparse
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

from uv_args import env_file_arg

REPLY = "Installation smoke check complete."


class LocalModel(BaseHTTPRequestHandler):
    calls = 0

    def log_message(self, *_args):
        pass  # Never log prompts or headers.

    def do_POST(self):
        if self.path.split("?", 1)[0] != "/v1/messages":
            self.send_error(404)
            return
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).calls += 1
        message = {"id": "msg_install", "type": "message", "role": "assistant",
                   "model": body["model"], "content": [{"type": "text", "text": REPLY}],
                   "stop_reason": "end_turn", "stop_sequence": None,
                   "usage": {"input_tokens": 10, "output_tokens": 8}}
        if body.get("stream"):
            events = [
                {"type": "message_start", "message": {**message, "content": [],
                                                        "stop_reason": None}},
                {"type": "content_block_start", "index": 0,
                 "content_block": {"type": "text", "text": ""}},
                {"type": "content_block_delta", "index": 0,
                 "delta": {"type": "text_delta", "text": REPLY}},
                {"type": "content_block_stop", "index": 0},
                {"type": "message_delta", "delta": {"stop_reason": "end_turn",
                                                        "stop_sequence": None},
                 "usage": {"output_tokens": 8}},
                {"type": "message_stop"},
            ]
            data = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)
            mime = "text/event-stream"
        else:
            data, mime = json.dumps(message), "application/json"
        encoded = data.encode()
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def isolated_env(root: Path) -> dict[str, str]:
    # Keep OS/tool discovery, discard the caller's model keys, DB URLs and product settings.
    keep = {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "USERPROFILE",
            "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)",
            "PROGRAMW6432", "HOMEDRIVE", "HOMEPATH", "TEMP", "TMP", "OS",
            "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"}
    env = {k: v for k, v in os.environ.items() if k.upper() in keep}
    cache = root / ".evowiki-run" / "install-cache"
    for key, folder in (("UV_CACHE_DIR", "uv"), ("UV_PYTHON_INSTALL_DIR", "python"),
                        ("npm_config_cache", "npm"), ("HF_HOME", "huggingface"),
                        ("FASTEMBED_CACHE_PATH", "fastembed"), ("PLAYWRIGHT_BROWSERS_PATH", "browser")):
        env[key] = str(cache / folder)
    env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8", EVOWIKI_NO_PAUSE="1",
               UV_HTTP_TIMEOUT="60", UV_HTTP_RETRIES="2")
    return env


def request(base: str, path: str, *, method="GET", body=None, raw=False):
    data = None if body is None else json.dumps(body).encode()
    req = Request(base + path, data=data, method=method,
                  headers={"Content-Type": "application/json", "X-EvoWiki-User": "install-smoke"})
    with urlopen(req, timeout=120) as response:
        text = response.read().decode()
    return text if raw else json.loads(text)


def verify_identity(root: Path, base: str, commit: str):
    health = request(base, "/health")
    assert health["commit"] == commit, health
    assert Path(health["instance_root"]).resolve() == root
    # Match the product health contract (routes_health.py and evowiki_ctl.py).
    assert health["protocol_version"] == 3 and not health["drained"]
    assert request(base, "/health/ready")["ok"] is True
    source = root / "obsidian-plugin"
    target = root / ".evowiki-vault" / ".obsidian" / "plugins" / "evowiki"
    for name in ("main.js", "manifest.json", "styles.css"):
        assert (target / name).stat().st_size > 0
        assert hashlib.sha256((source / name).read_bytes()).digest() == hashlib.sha256(
            (target / name).read_bytes()).digest(), name


def bootstrap_config(root: Path) -> dict[str, str]:
    """Resume only this check's failed bootstrap, retaining its database and vault."""
    report = json.loads((root / ".evowiki-run" / "install-check.json").read_text(encoding="utf-8"))
    if (report.get("ok") is not False or Path(report["root"]).resolve() != root
            or any(stage["name"] == "cpu-embedding" for stage in report["stages"])):
        raise RuntimeError("Resume requires this checkout's failed check before creating its test user.")
    config = {}
    for line in (root / ".env").read_text(encoding="utf-8").splitlines():
        key, value = line.split("=", 1)
        config[key] = json.loads(value)
    if (config.get("ANTHROPIC_API_KEY") != "ci-local-model-only"
            or config.get("DEEPSEEK_API_KEY") != ""
            or config.get("TAVILY_API_KEY") != ""
            or config.get("EVOWIKI_VAULT") != (root / ".evowiki-vault").as_posix()):
        raise RuntimeError("Resume requires the check's original synthetic configuration.")
    return config


def run(root: Path, *, timeout: int, resume_bootstrap: bool = False) -> dict:
    if os.name != "nt":
        raise RuntimeError("This check exercises the Windows launcher and must run on Windows.")
    saved_config = bootstrap_config(root) if resume_bootstrap else None
    for name in (() if resume_bootstrap else (".env", ".venv", ".pgdata", "obsidian-plugin/node_modules")):
        if (root / name).exists():
            raise RuntimeError(f"A fresh checkout is required; already exists: {root / name}")
    env = isolated_env(root)
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("Install uv before running this check.")
    launcher = [uv, "run", "--isolated", "--no-project", "--with", "python-dotenv==1.2.4",
                "python", str(root / "scripts" / "evowiki_run.py")]
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root,
                                     text=True, env=env).strip()
    report = {"ok": False, "commit": commit, "root": str(root), "stages": [],
              "resumed_bootstrap": resume_bootstrap}

    def stage(name, args):
        print(f"\n[{name}]", flush=True)
        started = time.monotonic()
        # Start outside the checkout to check root resolution as well as Unicode/spaces.
        subprocess.run(args, cwd=root.parent, env=env, check=True, timeout=timeout)
        report["stages"].append({"name": name, "seconds": round(time.monotonic() - started, 2)})

    port, db_port = free_port(), free_port()
    while db_port == port:
        db_port = free_port()
    base = f"http://127.0.0.1:{port}"
    LocalModel.calls = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), LocalModel)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        if not resume_bootstrap:
            stage("init", [*launcher, "init"])
        config = saved_config or {
            "EVOWIKI_DATABASE_URL": f"postgresql://evowiki_app:@127.0.0.1:{db_port}/postgres",
            "EVOWIKI_ADMIN_DATABASE_URL": f"postgresql://postgres:@127.0.0.1:{db_port}/postgres",
            "EVOWIKI_VAULT": (root / ".evowiki-vault").as_posix(),
            "ANTHROPIC_API_KEY": "ci-local-model-only", "DEEPSEEK_API_KEY": "",
            "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{server.server_port}",
            "EVOWIKI_RUNTIME_PROVIDER": "anthropic", "EVOWIKI_EMBED_PROVIDERS": "cpu",
            "EVOWIKI_FORGE": "off", "EVOWIKI_BOT": "off", "TAVILY_API_KEY": "",
            **{k: v.replace("\\", "/") for k, v in env.items()
               if k.endswith(("_DIR", "_PATH", "_HOME")) or k == "npm_config_cache"},
        }
        config["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{server.server_port}"
        (root / ".env").write_text("".join(f'{k}="{v}"\n' for k, v in config.items()),
                                    encoding="utf-8")
        # Detached Windows processes read the same explicit synthetic configuration.
        env.update(config)
        stage("doctor", [*launcher, "doctor"])
        stage("up", [*launcher, "up", "--port", str(port), "--no-bot"])
        verify_identity(root, base, commit)
        # Exercise the real CPU ONNX model, including its first uncached download.
        probe = ("import math; from evowiki.retrieval.embed import embedder_from_env, FastEmbedEmbedder; "
                 "e=embedder_from_env(); v=e.embed_query('Installation check'); "
                 "assert isinstance(e,FastEmbedEmbedder) and not e.on_gpu; "
                 "assert len(v)==e.dim and all(math.isfinite(x) for x in v); "
                 "print('CPU embedding verified:', e.id, 'dimensions:', len(v))")
        stage("cpu-embedding", [uv, "run", "--directory", root.as_posix(), "--no-sync", "--env-file",
                                env_file_arg(root / ".env"), "python", "-c", probe])
        request(base, "/users", method="POST", body={"user_id": "install-smoke",
                "survey": {"math": "none", "ml": "none", "dl": "none"}, "lang": "en"})
        budget = request(base, "/usage/settings")
        assert budget["daily_limit_usd"] == 5 and budget["monthly_limit_usd"] == 30
        request(base, "/usage/settings", method="PATCH", body={"confirm_budget": True})
        session = request(base, "/assistant/sessions", method="POST", body={})
        sid = session["session_id"]
        output = request(base, f"/assistant/sessions/{sid}/messages", method="POST",
                         body={"text": "Reply with the installation smoke check message."}, raw=True)
        assert "event: done" in output and REPLY in output, output[-1500:]
        assert LocalModel.calls > 0, "The product did not reach the local fake model."
        stage("status", [*launcher, "status", "--port", str(port)])
        stage("down", [*launcher, "down", "--port", str(port)])
        with socket.socket() as sock:
            assert sock.connect_ex(("127.0.0.1", port)) != 0, "Backend still listening after down."
        stage("restart", [*launcher, "up", "--port", str(port), "--no-bot"])
        verify_identity(root, base, commit)
        restored = request(base, f"/assistant/sessions/{sid}")
        assert restored["messages"][0]["text"].startswith("Reply with")
        assert restored["messages"][1]["text"] == REPLY
        vault = root / ".evowiki-vault"
        note = vault / "个人笔记 smoke.md"
        note.write_text("Personal installation note.\n", encoding="utf-8")
        plugin = vault / ".obsidian" / "plugins" / "evowiki"
        config_before = (root / ".env").read_bytes()
        plugin_data_before = (plugin / "data.json").read_bytes()
        stage("uninstall-preview", [*launcher, "uninstall", "--port", str(port)])
        assert (plugin / "main.js").is_file(), "Uninstall preview removed the plugin."
        stage("uninstall", [*launcher, "uninstall", "--apply", "--port", str(port)])
        assert not (plugin / "main.js").exists()
        assert "evowiki" not in json.loads((vault / ".obsidian" / "community-plugins.json").read_text(encoding="utf-8"))
        assert note.read_text(encoding="utf-8") == "Personal installation note.\n"
        assert (root / ".env").read_bytes() == config_before
        assert (plugin / "data.json").read_bytes() == plugin_data_before
        stage("reinstall", [*launcher, "up", "--port", str(port), "--no-bot"])
        verify_identity(root, base, commit)
        assert "evowiki" in json.loads((vault / ".obsidian" / "community-plugins.json").read_text(encoding="utf-8"))
        assert note.read_text(encoding="utf-8") == "Personal installation note.\n"
        assert (plugin / "data.json").read_bytes() == plugin_data_before
        assert request(base, f"/assistant/sessions/{sid}")["messages"] == restored["messages"]
        report.update(ok=True, local_model_calls=LocalModel.calls, paid_model_calls=0)
        return report
    finally:
        failed = sys.exc_info()[0] is not None
        try:
            stage("cleanup", [*launcher, "down", "--db", "--port", str(port)])
        except (OSError, subprocess.SubprocessError) as exc:
            report["ok"] = False
            report["cleanup_error"] = str(exc)
            if not failed:
                raise
            print(f"Cleanup also failed: {exc}", file=sys.stderr)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=10)
            dest = root / ".evowiki-run" / "install-check.json"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(json.dumps(report, indent=2), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="fresh independent Git checkout")
    parser.add_argument("--timeout", type=int, default=1800, help="timeout per command in seconds")
    parser.add_argument("--resume-bootstrap", action="store_true",
                        help="retain a failed check's data and retry before creating its test user")
    args = parser.parse_args()
    try:
        report = run(args.root.resolve(), timeout=args.timeout, resume_bootstrap=args.resume_bootstrap)
    except (OSError, RuntimeError, ValueError, AssertionError, subprocess.SubprocessError) as exc:
        print(f"Windows product installation check failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
