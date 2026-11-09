"""Fixed local setup protocol. Secrets enter only stdin and the canonical .env.

Model discovery uses read-only metadata requests; no generation or secret backups.
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from io import StringIO
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from dotenv.parser import parse_stream

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from evowiki.llm.catalog import PROVIDERS, configured, model_specs
from evowiki.llm.prices import load_prices
from evowiki.service.backup import BackupBusy, _file_lock

FIELDS = {"op", "expected_revision", "language", "provider", "api_key", "clear_key",
          "base_url", "model", "models", "prices_file", "forge_enabled", "backup_directory", "backup_keep"}


class SetupError(RuntimeError):
    def __init__(self, code, field="configuration"):
        self.code, self.field = code, field
        super().__init__(code)


def validate_url(address: str) -> None:
    try:
        u = urlsplit(address)
        if (u.scheme not in {"http", "https"} or not u.hostname or u.username
                or u.password or u.query or u.fragment):
            raise ValueError
        _ = u.port
    except ValueError:
        raise SetupError("invalid_url", "base_url") from None


class NoModelRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # A metadata endpoint must not forward credentials to any redirect target.
        return None


def discover_models(request: dict, env: dict[str, str]) -> dict:
    if set(request) - {"op", "language", "provider", "api_key", "base_url"}:
        raise SetupError("invalid_request")
    name = request.get("provider")
    if name not in PROVIDERS:
        raise SetupError("invalid_provider", "provider")
    draft = {**env, **proposed(request, env)}
    spec = PROVIDERS[name]
    address = draft.get(spec.base_url_env) or spec.base_url
    if not address:
        raise SetupError("required", "base_url")
    validate_url(address)
    key = draft.get(spec.key_env, "")
    if not key:
        raise SetupError("required", "api_key")
    headers = {"Accept": "application/json"}
    if name == "anthropic":
        headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
        endpoint = address.rstrip("/") + "/v1/models"
    else:
        headers["Authorization"] = "Bearer " + key
        endpoint = address.rstrip("/") + "/models"
    opener = build_opener(NoModelRedirects())
    deadline = time.monotonic() + 30
    cursor = None
    cursors = set()
    models = {}
    secrets = [draft.get(s.key_env, "") for s in PROVIDERS.values()]
    for _ in range(100):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SetupError("models_timeout", "models")
        query = {"limit": 1000, **({"after_id": cursor} if cursor else {})} if name == "anthropic" else {}
        url = endpoint + ("?" + urlencode(query) if query else "")
        try:
            with opener.open(Request(url, headers=headers), timeout=min(10, remaining)) as result:
                chunks = []
                size = 0
                while size <= 524288:
                    if time.monotonic() >= deadline:
                        raise SetupError("models_timeout", "models")
                    chunk = result.read1(min(65536, 524289 - size))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    size += len(chunk)
                payload = b"".join(chunks)
            if len(payload) > 524288:
                raise SetupError("models_invalid_response", "models")
            page = json.loads(payload)
        except HTTPError as exc:
            status = exc.code
            exc.close()
            code = "models_unauthorized" if status in {401, 403} else "models_unsupported" if status in {404, 405, 501} else "models_unavailable"
            raise SetupError(code, "models") from None
        except TimeoutError:
            raise SetupError("models_timeout", "models") from None
        except (URLError, OSError):
            raise SetupError("models_unavailable", "models") from None
        except (ValueError, UnicodeError):
            raise SetupError("models_invalid_response", "models") from None
        if not isinstance(page, dict) or not isinstance(page.get("data"), list):
            raise SetupError("models_invalid_response", "models")
        for item in page["data"]:
            model = item.get("id") if isinstance(item, dict) else None
            if (not isinstance(model, str) or not model or len(f"{name}/{model}") > 256
                    or any(c.isspace() or c in ",\x00" for c in model)
                    or any(secret and secret in model for secret in secrets)):
                raise SetupError("models_invalid_response", "models")
            models[model] = None
        more = page.get("has_more", False)
        if not isinstance(more, bool) or (name == "anthropic" and "has_more" not in page):
            raise SetupError("models_invalid_response", "models")
        if not more:
            if page.get("next_page") or page.get("next_page_token") or page.get("next"):
                raise SetupError("models_unsupported", "models")
            prefixes = {"anthropic": ("claude-",), "deepseek": ("deepseek-",),
                        "gemini": ("gemini-",), "kimi": ("kimi-", "moonshot-"), "glm": ("glm-",)}
            names = list(models)
            if name in prefixes:
                names = [m for m in names if m.lower().startswith(prefixes[name])]
            elif name == "openai":
                names = [m for m in names if (m.lower().startswith(("gpt-", "chatgpt-")) or re.match(r"o\d+(?:-|$)", m.lower()))
                         and not any(part in m.lower() for part in ("embedding", "whisper", "tts", "dall-e", "image", "sora", "moderation", "transcribe"))]
            if not names:
                raise SetupError("models_invalid_response", "models")
            if len(json.dumps(names)) > 500_000:
                raise SetupError("models_invalid_response", "models")
            return {"provider": name, "models": names}
        cursor = page.get("last_id")
        if name != "anthropic":
            raise SetupError("models_unsupported", "models")
        if not page["data"] or cursor != page["data"][-1]["id"] or cursor in cursors:
            raise SetupError("models_invalid_response", "models")
        cursors.add(cursor)
    raise SetupError("models_invalid_response", "models")


def revision(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def ensure_integrity(root: Path) -> None:
    marker = root / ".evowiki-run" / "setup-write.json"
    if marker.exists():
        raise SetupError("interrupted_write")


def parse_config(raw: bytes) -> dict[str, str]:
    try:
        text = raw.decode("utf-8")
        bindings = list(parse_stream(StringIO(text)))
        if any(b.error or (b.key and b.value is None) for b in bindings):
            raise ValueError
        keys = [b.key for b in bindings if b.key]
        if len(keys) != len(set(keys)):
            raise ValueError
        return {b.key: b.value for b in bindings if b.key}
    except (UnicodeError, ValueError):
        raise SetupError("invalid_env") from None


def repair_metadata(raw: bytes) -> dict[str, str]:
    """Recover complete single-line bindings; a repaired candidate must still parse whole."""
    try:
        text = raw.decode("utf-8")
    except UnicodeError:
        return {}
    recovered = {}
    for line in text.splitlines(keepends=True):
        bindings = list(parse_stream(StringIO(line)))
        if len(bindings) == 1 and not bindings[0].error and bindings[0].key and bindings[0].value is not None:
            recovered[bindings[0].key] = bindings[0].value
    return recovered


def validate(env: dict[str, str], root: Path, *, require_prices: bool = True) -> None:
    try:
        keep = int(env.get("EVOWIKI_BACKUP_KEEP", "14"))
        if not 1 <= keep <= 365:
            raise ValueError
    except ValueError:
        raise SetupError("invalid_value", "backup_keep") from None
    selected = env.get("EVOWIKI_RUNTIME_PROVIDER", "auto") or "auto"
    if selected not in {"auto", *PROVIDERS}:
        raise SetupError("invalid_provider", "provider")
    for name, spec in PROVIDERS.items():
        address = env.get(spec.base_url_env, "")
        if address:
            validate_url(address)
        if (name == "openai_compatible" and env.get(spec.key_env)
                and (not address or not env.get(spec.model_env))):
            raise SetupError("required", "base_url" if not address else "model")
    try:
        specs = model_specs(env)
    except ValueError:
        raise SetupError("invalid_model", "model") from None
    try:
        prices = load_prices()
        if custom := env.get("EVOWIKI_PRICES_FILE"):
            path = Path(custom)
            path = (path if path.is_absolute() else root / path).resolve()
            if root.resolve() not in path.parents or path.suffix.lower() != ".toml":
                raise ValueError
            prices = prices.overlay(load_prices(path))
    except (OSError, ValueError):
        raise SetupError("invalid_prices", "prices_file") from None
    for model in specs:
        if require_prices and configured(model.provider, env) and prices.price(model.provider, model.model) is None:
            raise SetupError("missing_price", "model")


def summary(env: dict[str, str]) -> dict:
    secrets = [env.get(spec.key_env, "") for spec in PROVIDERS.values()]
    def public(value: str) -> str:
        return "" if any(secret and secret in value for secret in secrets) else value
    def endpoint(value: str) -> str:
        try:
            parsed = urlsplit(value)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                return ""
            _ = parsed.port
        except ValueError:
            return ""
        return public(value)
    try:
        keep = int(env.get("EVOWIKI_BACKUP_KEEP", "14"))
    except ValueError:
        keep = None
    selected = env.get("EVOWIKI_RUNTIME_PROVIDER", "auto") or "auto"
    return {"local_options": {"forge_enabled": env.get("EVOWIKI_FORGE", "").strip().lower() not in {"off", "0", "false"},
                              "backup_directory": public(env.get("EVOWIKI_BACKUP_DIR", ".evowiki-run/backups")),
                              "backup_keep": keep},
            "selected_provider": selected if selected in {"auto", *PROVIDERS} else None,
            "providers": [{"id": name, "configured": configured(name, env),
                           "base_url": endpoint(env.get(s.base_url_env) or s.base_url),
                           "model": public(env.get(s.model_env) or s.default_model),
                           "models": [public(v.strip()) for v in env.get(s.models_env, "").split(",") if v.strip()]}
                          for name, s in PROVIDERS.items()]}


def proposed(request: dict, env: dict[str, str], root: Path = ROOT) -> dict[str, str]:
    name = request.get("provider")
    provider_fields = {"api_key", "clear_key", "base_url", "model", "models"}
    if name is None and not provider_fields.intersection(request):
        updates = {}
    elif name not in PROVIDERS:
        raise SetupError("invalid_provider", "provider")
    else:
        updates = {"EVOWIKI_RUNTIME_PROVIDER": name}
    if "forge_enabled" in request:
        if not isinstance(request["forge_enabled"], bool):
            raise SetupError("invalid_value", "forge_enabled")
        updates["EVOWIKI_FORGE"] = "on" if request["forge_enabled"] else "off"
    if "backup_keep" in request:
        keep = request["backup_keep"]
        if isinstance(keep, bool) or not isinstance(keep, int) or not 1 <= keep <= 365:
            raise SetupError("invalid_value", "backup_keep")
        updates["EVOWIKI_BACKUP_KEEP"] = str(keep)
    if "backup_directory" in request:
        directory = request["backup_directory"]
        if not isinstance(directory, str) or not directory.strip() or any(c in directory for c in "\r\n\x00"):
            raise SetupError("invalid_value", "backup_directory")
        target = Path(directory)
        target = (target if target.is_absolute() else root / target).resolve()
        if directory.lower() != "off" and any((p / ".obsidian").is_dir() for p in (target, *target.parents)):
            raise SetupError("protected_directory", "backup_directory")
        updates["EVOWIKI_BACKUP_DIR"] = directory
    if "prices_file" in request:
        value = request["prices_file"]
        if not isinstance(value, str) or any(c in value for c in "\r\n\x00"):
            raise SetupError("invalid_value", "prices_file")
        updates["EVOWIKI_PRICES_FILE"] = value.strip()
    if name is None:
        return updates
    spec = PROVIDERS[name]
    for field, key in (("api_key", spec.key_env), ("base_url", spec.base_url_env),
                       ("model", spec.model_env), ("prices_file", "EVOWIKI_PRICES_FILE")):
        if field in request:
            value = request[field]
            if not isinstance(value, str) or any(c in value for c in "\r\n\x00"):
                raise SetupError("invalid_value", field)
            if field != "api_key" or value:
                updates[key] = value.strip()
            if field == "model" and (len(value) > 256 or any(c.isspace() or c == "," for c in value)):
                raise SetupError("invalid_model", field)
    if "clear_key" in request and not isinstance(request["clear_key"], bool):
        raise SetupError("invalid_value", "clear_key")
    if request.get("clear_key"):
        if request.get("api_key"):
            raise SetupError("conflicting_fields", "api_key")
        updates[spec.key_env] = ""
    if "models" in request:
        values = request["models"]
        if not isinstance(values, list) or any(not isinstance(v, str) or not v.strip()
                or any(c in v for c in ",\r\n\x00") for v in values):
            raise SetupError("invalid_value", "models")
        updates[spec.models_env] = ",".join(v.strip() for v in values)
    return updates


def render(raw: bytes, updates: dict[str, str]) -> bytes:
    lines = raw.decode("utf-8").splitlines(keepends=True)
    remaining = dict(updates)
    out = []
    for line in lines:
        match = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        key = match[1] if match else None
        if key in remaining:
            value = remaining.pop(key).replace("\\", "\\\\").replace("'", "\\'")
            # Keep comments outside quoted values, including on edited settings.
            quote = None
            escaped = False
            comment = ""
            for index, char in enumerate(line):
                if escaped:
                    escaped = False
                elif char == "\\" and quote:
                    escaped = True
                elif char in {"'", '"'}:
                    quote = None if quote == char else char if quote is None else quote
                elif char == "#" and quote is None and index and line[index - 1].isspace():
                    comment = " " + line[index:].rstrip("\r\n")
                    break
            out.append(f"{key}='{value}'{comment}\n")
        else:
            out.append(line)
    if out and not out[-1].endswith("\n"):
        out.append("\n")
    for key, value in remaining.items():
        value = value.replace("\\", "\\\\").replace("'", "\\'")
        out.append(f"{key}='{value}'\n")
    return "".join(out).encode("utf-8")


def runtime(root: Path, op: str) -> dict:
    python = root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.is_file():
        raise SetupError("runtime_missing", "restart")
    try:
        result = subprocess.run([str(python), str(root / "scripts" / "evowiki_setup_runtime.py"), op],
                                cwd=root, capture_output=True, text=True, encoding="utf-8", timeout=600, check=False,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        response = json.loads(result.stdout)
        if not response.get("ok"):
            raise SetupError(response.get("code", "restart_failed"), "restart")
        return response["restart"]
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise SetupError("restart_failed", "restart") from None


def handle(request: dict, root: Path = ROOT, control=runtime) -> dict:
    response = {"ok": False, "code": "invalid_request", "errors": [],
                "restart": {"state": "unchanged", "port": None}, **summary({})}
    try:
        if (not isinstance(request, dict) or set(request) - FIELDS
                or request.get("op") not in {"status", "check", "apply", "models"}
                or request.get("language", "en") not in {"zh", "en"}):
            raise SetupError("invalid_request")
        path = root / ".env"
        if path.resolve() != root.resolve() / ".env" or (root / ".evowiki-run").resolve() != root.resolve() / ".evowiki-run":
            raise SetupError("unsafe_configuration_path")
        raw = path.read_bytes() if path.exists() else b""
        response.update(revision=revision(raw))
        if request["op"] == "models":
            ensure_integrity(root)
            catalog = discover_models(request, repair_metadata(raw))
            response.update(ok=True, code="ok", model_catalog=catalog)
            return response
        if request["op"] == "status":
            response.update(**summary(repair_metadata(raw)))
        ensure_integrity(root)
        try:
            env = parse_config(raw)
        except SetupError:
            if request["op"] == "status":
                raise
            # Only supplied fixed fields can be repaired. All other lines survive and
            # the complete candidate must be valid; unknown broken lines fail closed.
            updates = proposed(request, repair_metadata(raw), root)
            env = parse_config(render(raw, updates))
        if request["op"] == "status":
            response.update(**summary(env))
        if request["op"] == "status":
            if set(request) - {"op", "language"}:
                raise SetupError("invalid_request")
            validate(env, root)
            response.update(**summary(env))
        else:
            updates = proposed(request, env, root)
            new = {**env, **updates}
            validate(new, root)
            if request["op"] == "apply":
                if request.get("expected_revision") != revision(raw):
                    raise SetupError("revision_conflict", "expected_revision")
                restart = control(root, "inspect")
                run = root / ".evowiki-run"
                run.mkdir(exist_ok=True)
                lock = run / "setup.lock"
                with _file_lock(lock):
                    ensure_integrity(root)
                    if (path.read_bytes() if path.exists() else b"") != raw:
                        raise SetupError("revision_conflict", "expected_revision")
                    payload = render(raw, updates)
                    if parse_config(payload) != new:
                        raise SetupError("invalid_env")
                    marker = run / "setup-write.json"
                    # Exclusive byte lock on the sole secret file; no second plaintext copy.
                    with path.open("r+b" if path.exists() else "w+b") as target:
                        if os.name == "nt":
                            import msvcrt
                            msvcrt.locking(target.fileno(), msvcrt.LK_NBLCK, max(1, len(raw)))
                        else:
                            import fcntl
                            fcntl.flock(target.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        if target.read() != raw:
                            raise SetupError("revision_conflict", "expected_revision")
                        with marker.open("w", encoding="utf-8") as marker_file:
                            marker_file.write(json.dumps({"expected_hash": revision(payload)}))
                            marker_file.flush()
                            os.fsync(marker_file.fileno())
                        target.seek(0)
                        target.write(payload)
                        target.truncate()
                        target.flush()
                        os.fsync(target.fileno())
                        target.seek(0)
                        if target.read() != payload or not os.path.samestat(path.stat(), os.fstat(target.fileno())):
                            raise SetupError("interrupted_write")
                    marker.unlink()
                    response.update(revision=revision(payload), **summary(new))
                    response["restart"] = control(root, "restart") if restart["state"] == "running" else restart
            else:
                response.update(**summary(new))
        response.update(ok=True, code="ok")
    except BackupBusy:
        response.update(code="setup_busy", errors=[{"field": "configuration", "code": "setup_busy"}])
    except SetupError as exc:
        response.update(code=exc.code, errors=[{"field": exc.field, "code": exc.code}])
        if exc.field == "restart":
            response["restart"] = {"state": "failed", "port": response["restart"].get("port")}
    except (OSError, ValueError):
        response.update(code="configuration_unavailable", errors=[{"field": "configuration", "code": "configuration_unavailable"}])
    return response


def main() -> int:
    try:
        request = json.loads(sys.stdin.read(65537))
        result = handle(request)
    except (ValueError, UnicodeError):
        result = {"ok": False, "code": "invalid_request", "errors": []}
    print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
