"""Select the runtime dependencies before uv touches the project's environment."""

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

if __name__ == "__main__":
    sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))

from evowiki_args import launcher_parser, parse_launcher_args
from launcher_i18n import cli_format, cli_text, print, set_language


def show_help(root: Path, args: list[str]) -> int:
    """Help never enters configuration, dependency syncing or process lifecycle code."""
    if "--language" in args:
        index = args.index("--language")
        if index + 1 < len(args) and args[index + 1] in {"zh", "en"}:
            set_language(args[index + 1])
    for argument in args:
        if argument in {"--language=en", "--language=zh"}:
            set_language(argument.split("=", 1)[1])
    try:
        launcher_parser(wrapper=True, root=root).parse_args(args or ["--help"])
    except SystemExit as exc:
        return int(exc.code)
    return 0


# Keep even a fresh checkout's help independent of dotenv and local bytecode caches.
if __name__ == "__main__":
    if len(sys.argv) == 1 or any(a in ("--help", "-h") for a in sys.argv[1:]):
        sys.exit(show_help(Path(__file__).resolve().parent.parent, sys.argv[1:]))
    parse_launcher_args(sys.argv[1:], wrapper=True)

from envfile import read_env_file
from git_runtime import git_runtime_dir

VC_RUNTIME_HELP = ("请从 https://aka.ms/vc14/vc_redist.x64.exe 安装 Microsoft Visual C++ v14 x64 运行库；"
                   "需要管理员批准，无权限时请联系管理员，然后重新打开终端运行 doctor")


# Kept dependency-free: doctor also runs before project dependencies are installed.
MODEL_KEYS = {
    "anthropic": ("ANTHROPIC_API_KEY", "Claude"),
    "deepseek": ("DEEPSEEK_API_KEY", "DeepSeek"),
    "openai": ("OPENAI_API_KEY", "OpenAI"),
    "gemini": ("GEMINI_API_KEY", "Gemini"),
    "kimi": ("MOONSHOT_API_KEY", "Kimi"),
    "glm": ("ZAI_API_KEY", "GLM"),
    "openai_compatible": ("EVOWIKI_COMPATIBLE_API_KEY", "OpenAI 兼容服务"),
}


def model_configuration_error(env: dict[str, str]) -> str | None:
    mode = env.get("EVOWIKI_RUNTIME_PROVIDER", "auto") or "auto"
    if mode not in ("auto", *MODEL_KEYS):
        return cli_text("EVOWIKI_RUNTIME_PROVIDER 必须为 auto 或 ") + ", ".join(MODEL_KEYS)
    selected = MODEL_KEYS.values() if mode == "auto" else (MODEL_KEYS[mode],)
    if not any(env.get(key, "").strip() for key, _ in selected):
        return "尚未配置模型密钥；请在 .env 填写所选供应商的 API_KEY（参照 .env.example）"
    if mode == "openai_compatible" or (mode == "auto" and env.get("EVOWIKI_COMPATIBLE_API_KEY")
            and not any(env.get(key) for provider, (key, _) in MODEL_KEYS.items()
                        if provider != "openai_compatible")):
        if not env.get("EVOWIKI_COMPATIBLE_BASE_URL", "").strip():
            return "OpenAI 兼容服务需要 EVOWIKI_COMPATIBLE_BASE_URL"
        if not env.get("EVOWIKI_COMPATIBLE_MODEL", "").strip():
            return "OpenAI 兼容服务需要 EVOWIKI_COMPATIBLE_MODEL"
    return None


def initial_setup_needed(env: dict[str, str]) -> bool:
    """A missing selected key is setup state; never silently select another vendor."""
    mode = env.get("EVOWIKI_RUNTIME_PROVIDER", "auto") or "auto"
    if mode == "auto":
        return not any(env.get(key, "").strip() for key, _ in MODEL_KEYS.values())
    return mode in MODEL_KEYS and not env.get(MODEL_KEYS[mode][0], "").strip()


def missing_vc_runtime() -> list[str]:
    """Check the official system runtime without importing project dependencies."""
    if sys.platform != "win32":
        return []
    import ctypes

    missing = []
    for name in ("msvcp140.dll", "msvcp140_1.dll", "vcruntime140.dll", "vcruntime140_1.dll"):
        try:
            ctypes.WinDLL(str(Path(os.environ["SystemRoot"]) / "System32" / name))
        except OSError:
            missing.append(name)
    return missing


def vault_ownership_error(vault: Path, root: Path) -> bool:
    """Existing user vaults are opt-in; managed vaults must belong to this instance."""
    marker = vault / ".evowiki-owner.json"
    if not marker.exists():
        return False
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
        return (not isinstance(data, dict) or not isinstance(data.get("instance_root"), str)
                or not data["instance_root"]
                or Path(data["instance_root"]).resolve() != root.resolve())
    except (OSError, ValueError):
        return True


def initialize(root: Path) -> bool:
    """Write first-run files only. Existing settings and notes are never changed."""
    created = False
    env_file = root / ".env"
    if not env_file.exists():
        with env_file.open("x", encoding="utf-8", newline="\n") as target:
            target.write((root / ".env.example").read_text(encoding="utf-8"))
        created = True
    vault = root / ".evowiki-vault" / ".obsidian"
    new_vault = not vault.parent.exists()
    if root.resolve() not in vault.parent.resolve().parents:
        raise RuntimeError("默认笔记库指向仓库之外；请移走该链接，或在 .env 中指定独立 EVOWIKI_VAULT")
    if vault_ownership_error(vault.parent, root):
        raise RuntimeError("默认笔记库属于其他实例或归属记录损坏；请选择独立 EVOWIKI_VAULT")
    vault.mkdir(parents=True, exist_ok=True)
    plugins = vault / "community-plugins.json"
    if not plugins.exists():
        with plugins.open("x", encoding="utf-8", newline="\n") as target:
            target.write('["evowiki"]\n')
    owner = vault.parent / ".evowiki-owner.json"
    if new_vault and not owner.exists():
        owner.write_text(json.dumps({"instance_root": str(root.resolve())}), encoding="utf-8")
    return created


def diagnose(root: Path, env: dict[str, str]) -> int:
    """No imports from the project environment, model downloads or paid requests."""
    failures = []
    for name in ("git", "uv", "node", "npm", "powershell"):
        exe = shutil.which(name)
        if not exe:
            failures.append(name)
            print(cli_format('必需 {p0}：未安装；按 README 安装后重试 doctor', p0=name))
            continue
        cmd = [exe, "-NoProfile", "-Command", "$PSVersionTable.PSVersion.ToString()"] if name == "powershell" else [exe, "--version"]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15, check=False)
            version = result.stdout.strip().splitlines()[0] if result.stdout.strip() else "无法读取版本"
            ok = result.returncode == 0
            if name == "node":
                match = re.match(r"v?(\d+)", version)
                ok = ok and match is not None and int(match[1]) >= 22
            if name == "uv" and ok:
                help_result = subprocess.run([exe, "run", "--help"], capture_output=True, text=True,
                                             encoding="utf-8", errors="replace", timeout=15, check=False)
                ok = help_result.returncode == 0 and all(flag in help_result.stdout for flag in ("--locked", "--exact", "--isolated", "--no-project"))
            if not ok:
                failures.append(name)
            print(cli_format('必需 {p0}：{p1}{p2}', p0=name, p1=version, p2='' if ok else '；无法使用，请按 README 安装（Node >=22）'))
        except (OSError, subprocess.TimeoutExpired):
            failures.append(name)
            print(cli_format('必需 {p0}：版本检查失败；按 README 重新安装', p0=name))
    if sys.platform == "win32":
        runtime = git_runtime_dir()
        if runtime is None:
            failures.append("Git runtime")
            print("必需 PostgreSQL 运行库：缺失；请安装 README 链接的完整官方 Git for Windows，"
                  "重新打开终端后重试 doctor")
        else:
            print(cli_format('必需 PostgreSQL 运行库：{p0}（来自 Git for Windows）', p0=runtime))
        missing_vc = missing_vc_runtime()
        if missing_vc:
            failures.append("Visual C++ runtime")
            print(cli_format('必需向量模型运行库：缺少 {p0}；{p1}', p0=', '.join(missing_vc), p1=VC_RUNTIME_HELP))
        else:
            print("必需向量模型运行库：Microsoft Visual C++ v14 x64 已安装")
    print(cli_format('配置文件：{p0}', p0='已创建' if (root / '.env').is_file() else '缺失，请先运行 init'))
    if not (root / '.env').is_file():
        failures.append("config")
    try:
        url = urlsplit(env.get("EVOWIKI_DATABASE_URL", ""))
        valid_db = url.scheme in ("postgresql", "postgres") and url.hostname in ("localhost", "127.0.0.1") and bool(url.path.strip('/'))
        port = url.port or 5432
    except ValueError:
        valid_db, port = False, 0
    if not valid_db:
        failures.append("database")
    print(cli_format('数据库地址：{p0}', p0='已配置本地数据库（访问未验证）' if valid_db else '无效；参照 .env.example 配置本地 PostgreSQL URL'))
    for name, label in (*MODEL_KEYS.values(), ("TAVILY_API_KEY", "联网搜索")):
        print(cli_format("{p0}：{p1}", p0=label, p1="已配置" if env.get(name) else "未配置"))
    if error := model_configuration_error(env):
        if initial_setup_needed(env):
            print("模型未配置：本地组件可以启动；请在应用中完成首次设置。")
        else:
            failures.append("model")
            print(cli_format('必需模型：{p0}', p0=error))
    from evowiki_setup import SetupError, validate
    try:
        validate(env, root, require_prices=False)
    except SetupError as exc:
        failures.append("configuration syntax")
        print(cli_format('配置错误：{p0}: {p1}', p0=exc.field, p1=exc.code))
    vault = Path(env.get("EVOWIKI_VAULT") or root / ".evowiki-vault")
    vault = vault if vault.is_absolute() else root / vault
    for label, path in (("仓库", root), ("笔记库", vault)):
        try:
            if label == "笔记库" and vault_ownership_error(path.resolve(), root):
                failures.append("protected vault")
                print(cli_format('笔记库：{p0}（受保护，只读；请选择独立 vault）', p0=path.resolve()))
                continue
            if label == "笔记库" and not (path / ".obsidian").is_dir():
                raise OSError("不是 Obsidian 库")
            with tempfile.TemporaryFile(dir=path):
                pass
            print(cli_format('{p0}：{p1}（可写）', p0=label, p1=path.resolve()))
        except OSError:
            failures.append(label)
            print(cli_format('{p0}：{p1}（不存在、无写权限或没有 .obsidian；运行 init / 检查 EVOWIKI_VAULT）', p0=label, p1=path.resolve()))
    if valid_db:
        with socket.socket() as probe:
            probe.settimeout(1)
            taken = probe.connect_ex(("127.0.0.1", port)) == 0
        if taken:
            try:
                lines = (root / ".pgdata" / "postmaster.pid").read_text(encoding="utf-8").splitlines()
                owned = Path(lines[1]).resolve() == (root / ".pgdata").resolve() and int(lines[3]) == port
            except (OSError, ValueError, IndexError):
                owned = False
            print(cli_format('数据库端口 {p0}：{p1}', p0=port, p1='本目录数据库已监听（认证未验证）' if owned else '已占用且归属未确认；运行 status，确认后修改数据库端口'))
            if not owned:
                failures.append("database ownership")
    print(cli_format('本地版面解析：{p0}', p0='已配置' if env.get('EVOWIKI_LAYOUT_PYTHON') else '未配置（可选）'))
    print(cli_format('向量设备：{p0}', p0=env.get('EVOWIKI_EMBED_PROVIDERS') or 'cpu'))
    if env.get("EVOWIKI_EMBED_PROVIDERS", "cpu") not in ("", "cpu", "cuda"):
        failures.append("embedding device")
        print("向量配置无效：EVOWIKI_EMBED_PROVIDERS 必须为 cpu 或 cuda")
    try:
        result = subprocess.run(["git", "rev-parse", "--verify", "HEAD"], cwd=root, capture_output=True,
                                text=True, encoding="utf-8", errors="replace", timeout=10, check=False)
        print(cli_format('构建提交：{p0}', p0=result.stdout.strip() if result.returncode == 0 else '未验证（尚无 Git 提交）'))
    except (OSError, subprocess.TimeoutExpired):
        print("构建提交：未验证（无法运行 Git）")
    print("以上只检查安装和配置；密钥有效性、账户余额及 GPU 实际执行仍需运行时验证。")
    print(cli_format('诊断：{p0}', p0='缺少必需项，请修复后重试 doctor' if failures else '必需配置检查通过；运行 up 验证服务'))
    return 1 if failures else 0


def runtime_args(env: dict[str, str], root: Path | None = None) -> list[str]:
    device = env.get("EVOWIKI_EMBED_PROVIDERS", "cpu") or "cpu"
    if device not in ("cpu", "cuda"):
        raise ValueError("EVOWIKI_EMBED_PROVIDERS 必须是 cpu 或 cuda")
    extra = "embed-gpu" if device == "cuda" else "embed"
    args = ["--locked", "--exact", "--extra", "llm", "--extra", extra, "--extra", "deep"]
    # Both wheels share files. Repair only a missing/wrong runtime; reinstalling DLLs
    # on every `up` would fail while an existing Windows backend holds them open.
    package = "onnxruntime-gpu" if device == "cuda" else "onnxruntime"
    python = root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python") if root else None
    probe = (
        "import importlib.metadata as m; import onnxruntime as o; import sys; "
        "gpu_installed = any(d.metadata['Name'] == 'onnxruntime-gpu' for d in m.distributions()); "
        f"sys.exit(('CUDAExecutionProvider' in o.get_available_providers()) != {device == 'cuda'} "
        f"or ({device == 'cpu'} and gpu_installed))")
    if python is not None and (not python.is_file() or subprocess.run(
            [str(python), "-c", probe], stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, check=False).returncode != 0):
        args += ["--reinstall-package", package]
    return args


def main() -> int:
    here = Path(__file__).resolve().parent
    args = sys.argv[1:]
    if not args or any(a in ("--help", "-h") for a in args):
        return show_help(here.parent, args)
    parsed = parse_launcher_args(args, wrapper=True, root=here.parent)
    # Locale is inherited through the launcher environment. Remove this grammar-only
    # flag before forwarding backup arguments or interpreting the command position.
    forwarded = []
    skip = False
    for argument in args:
        if skip:
            skip = False
        elif argument == "--language":
            skip = True
        elif not argument.startswith("--language="):
            forwarded.append(argument)
    args = forwarded
    if parsed.cmd == "setup":
        from evowiki_setup import main as setup_main
        return setup_main()
    try:
        main_root = Path(subprocess.check_output(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=here, text=True, encoding="utf-8", stderr=subprocess.DEVNULL).strip()).parent
    except (OSError, subprocess.CalledProcessError):
        if parsed.cmd != "doctor":
            raise RuntimeError("无法识别 Git 仓库；请安装 Git，并在完整 checkout 中运行") from None
        main_root = here.parent
    if parsed.cmd == "init":
        initialize(main_root)
        print("初始化完成。运行 EvoWiki.exe，然后在应用中完成首次设置。")
        return 0
    if parsed.cmd in ("up", "doctor"):
        from evowiki_setup import ensure_integrity, parse_config
        ensure_integrity(main_root)
        file_env = parse_config((main_root / ".env").read_bytes()) if (main_root / ".env").exists() else {}
    else:
        file_env = read_env_file(main_root / ".env", missing_ok=True)
    env = {**file_env, **os.environ}
    if parsed.cmd == "doctor":
        return diagnose(main_root, env)
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("找不到 uv；请按 README 安装后重试")
    if parsed.cmd == "up":
        if os.name != "nt":
            raise RuntimeError("一键后台启动目前支持 Windows；其他系统请按 docs/runtime.md 手动运行服务")
        if initialize(main_root):
            env = {**parse_config((main_root / ".env").read_bytes()), **os.environ}
            print("已创建配置和空白笔记库。")
        from evowiki_setup import ensure_integrity, validate
        ensure_integrity(main_root)
        validate(env, main_root, require_prices=False)
        if (error := model_configuration_error(env)) and not initial_setup_needed(env):
            raise RuntimeError(error)
        if initial_setup_needed(env):
            print("请在应用中完成首次设置。学习模型尚未配置。")
        if not env.get("EVOWIKI_DATABASE_URL"):
            raise RuntimeError(".env 缺少 EVOWIKI_DATABASE_URL，请参照 .env.example 填写本地数据库地址")
        missing = [name for name in ("node", "npm", "powershell") if not shutil.which(name)]
        if missing:
            raise RuntimeError(cli_format('缺少运行工具：{p0}；请按 README 安装后重试', p0=', '.join(missing)))
        if git_runtime_dir() is None:
            raise RuntimeError("PostgreSQL 缺少 Git for Windows 运行库；请重新安装完整官方 Git，"
                               "重新打开终端后运行 doctor")
        missing_vc = missing_vc_runtime()
        if missing_vc:
            raise RuntimeError(cli_format('向量模型缺少运行库 {p0}；{p1}', p0=', '.join(missing_vc), p1=VC_RUNTIME_HELP))
    # Inspect/stop the existing runtime before uv may replace files it has open.
    reuse = False
    if parsed.cmd == "up":
        python = main_root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        state = main_root / ".evowiki-run" / "state.json"
        if python.is_file():
            preflight = ["preflight", "--port", str(parsed.port), "--from", parsed.from_]
            if parsed.root is not None:
                served_root = parsed.root if parsed.root.is_absolute() else main_root / parsed.root
                preflight = ["--root", str(served_root), *preflight]
            if parsed.vault is not None:
                preflight += ["--vault", str(parsed.vault)]
            if parsed.no_bot:
                preflight.append("--no-bot")
            if parsed.resume:
                preflight.append("--resume")
            rc = subprocess.call([str(python), str(main_root / "scripts" / "evowiki_ctl.py"), *preflight])
            if rc not in (0, 75):
                return rc
            reuse = rc == 0
        elif state.exists() and any(json.loads(state.read_text(encoding="utf-8")).get(key) for key in ("backend", "supervisor")):
            raise RuntimeError("已有运行记录，但项目 Python 缺失；请先从原运行环境安全停止后端，不能覆盖正在运行的依赖")
    if parsed.cmd in ("backup", "backups", "restore-check", "restore"):
        restore_confirmed = parsed.cmd == "restore" and parsed.confirm
        if restore_confirmed:
            rc = subprocess.call([uv, "run", "--directory", str(main_root), "--no-sync", "python",
                                  str(main_root / "scripts" / "evowiki_ctl.py"), "down"])
            if rc:
                return rc
        command = "list" if parsed.cmd == "backups" else parsed.cmd
        return subprocess.call([uv, "run", "--directory", str(main_root), "--no-sync", "python",
                                str(main_root / "scripts" / "evowiki_backup.py"), "--env", str(main_root / ".env"), command, *args[1:]])
    # Stop/status/backup never sync dependencies. Network requests have bounded retries.
    os.environ.setdefault("UV_HTTP_TIMEOUT", "60")
    os.environ.setdefault("UV_HTTP_RETRIES", "2")
    options = runtime_args(env, main_root) if parsed.cmd == "up" and not reuse else ["--no-sync"]
    remove_runtime = parsed.cmd == "uninstall" and parsed.remove_runtime
    if remove_runtime and Path(sys.prefix).resolve() == (main_root / ".venv").resolve():
        raise RuntimeError("删除运行环境请使用独立解释器：uv run --isolated --no-project --with python-dotenv==1.2.4 python scripts/evowiki_run.py uninstall --remove-runtime（确认预览后加 --apply）")
    rc = subprocess.call([uv, "run", "--directory", main_root.as_posix(), *options,
                          "python", str(main_root / "scripts" / "evowiki_ctl.py"), *args])
    if rc == 0 and remove_runtime and parsed.apply:
        target = main_root / ".venv"
        if target.exists():
            if target.is_symlink() or target.is_junction() or main_root.resolve() not in target.resolve().parents:
                raise RuntimeError("运行环境路径超出本实例边界，拒绝删除")
            shutil.rmtree(target.resolve())
            print(cli_format('已删除本实例运行环境：{p0}', p0=target.resolve()))
    return rc


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(cli_format('启动失败：{p0}', p0=exc), file=sys.stderr)
        sys.exit(1)
