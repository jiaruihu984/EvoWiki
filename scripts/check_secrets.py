"""Scan publishable working files and every reachable Git ref; report metadata only.

Run `python scripts/check_secrets.py --download` to install the pinned official Gitleaks
binary after SHA256 verification. Ignored local files (including .env) are not publishable
working files. History scanning still checks any past tracked version of those files.
"""

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION = "8.30.1"
TEMPLATE = ('{{range .}}{"path":{{printf "%q" .File}},"commit":{{printf "%q" .Commit}},'
            '"rule":{{printf "%q" .RuleID}}}{{"\\n"}}{{end}}')


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT).decode("utf-8").strip()


def download(folder: Path) -> Path:
    system = platform.system().lower()
    arch = {"amd64": "x64", "x86_64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine().lower())
    if system not in {"windows", "linux", "darwin"} or arch is None:
        raise RuntimeError("unsupported Gitleaks download platform; install Gitleaks and pass --gitleaks")
    folder.mkdir(parents=True, exist_ok=True)
    binary = folder / ("gitleaks.exe" if system == "windows" else "gitleaks")
    suffix = "zip" if system == "windows" else "tar.gz"
    name = f"gitleaks_{VERSION}_{system}_{arch}.{suffix}"
    base = f"https://github.com/gitleaks/gitleaks/releases/download/v{VERSION}/"

    def read(name: str) -> bytes:
        req = urllib.request.Request(base + name, headers={"User-Agent": "EvoWiki-secret-check"})
        with urllib.request.urlopen(req, timeout=60) as response:
            return response.read()

    checksums = read(f"gitleaks_{VERSION}_checksums.txt").decode("utf-8")
    expected = next(line.split()[0] for line in checksums.splitlines() if line.endswith("  " + name))
    payload = read(name)
    if hashlib.sha256(payload).hexdigest() != expected:
        raise RuntimeError("Gitleaks download checksum mismatch")
    archive = folder / name
    archive.write_bytes(payload)
    if suffix == "zip":
        with zipfile.ZipFile(archive) as package:
            binary.write_bytes(package.read(binary.name))
    else:
        with tarfile.open(archive) as package, package.extractfile(binary.name) as source:
            binary.write_bytes(source.read())
        binary.chmod(0o755)
    return binary


def scan(binary: Path, scope: str, output: Path) -> dict:
    report = output / f"{scope}.jsonl"
    template = output / "metadata.tmpl"
    template.write_text(TEMPLATE, encoding="utf-8")
    common = ["--config", str(ROOT / ".gitleaks.toml"), "--redact=100", "--no-banner", "--no-color",
              "--ignore-gitleaks-allow", "--log-level=error", "--report-format=template",
              "--report-template", str(template), "--report-path", str(report),
              "--max-archive-depth=2", "--timeout=900"]
    count = None
    with tempfile.TemporaryDirectory(prefix="scan-", dir=output) as scratch:
        stage = Path(scratch)
        if scope == "history":
            command = ["git", "--log-opts=--all --full-history", str(ROOT)]
        else:
            options = ["-z"] if scope == "tracked" else ["-co", "--exclude-standard", "-z"]
            names = set(git("ls-files", *options).split("\0")) - {""}
            count = 0
            for name in sorted(names):
                source = ROOT / name
                if not source.is_file() or source.is_symlink():
                    continue
                target = stage / name
                target.parent.mkdir(parents=True, exist_ok=True)
                os.link(source, target)
                count += 1
            command = ["dir", str(stage)]
        # Never forward scanner diagnostics: even redaction can retain surrounding private prose.
        result = subprocess.run([str(binary), *command, *common], cwd=ROOT, capture_output=True,
                                timeout=960, check=False)
        if result.returncode not in {0, 1} or not report.exists():
            raise RuntimeError(f"Gitleaks {scope} scan did not complete (exit {result.returncode})")
        findings = [json.loads(line) for line in report.read_text(encoding="utf-8").splitlines() if line.strip()]
        for finding in findings:
            name = finding["path"].replace("\\", "/")
            prefix = stage.as_posix() + "/"
            finding["path"] = name.removeprefix(prefix)
        report.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in findings), encoding="utf-8")
    return {"scope": scope, "files": count, "findings": len(findings), "exit_code": result.returncode}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gitleaks", type=Path)
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--report-dir", type=Path, default=ROOT / ".evowiki-run" / "security")
    parser.add_argument("--scope", choices=("all", "tracked", "working", "history"), default="all")
    args = parser.parse_args(argv)
    try:
        output = args.report_dir.resolve()
        output.mkdir(parents=True, exist_ok=True)
        binary = args.gitleaks or (Path(found) if (found := shutil.which("gitleaks")) else None)
        if args.download:
            binary = download(output / "tools")
        if binary is None:
            raise RuntimeError("install Gitleaks or run this command with --download")
        binary = binary.resolve()
        actual = subprocess.check_output([str(binary), "version"], text=True).strip()
        if actual != VERSION:
            raise RuntimeError(f"Gitleaks {VERSION} is required; run with --download")
        # Working files include every tracked file; scan the large fixtures once.
        scopes = ("working", "history") if args.scope == "all" else (args.scope,)
        results = [scan(binary, scope, output) for scope in scopes]
        summary = {"gitleaks": VERSION, "head": git("rev-parse", "HEAD"),
                   "tracked_files": len(set(git("ls-files", "-z").split("\0")) - {""}),
                   "reachable_commits": int(git("rev-list", "--all", "--count")),
                   "refs": len(git("for-each-ref", "--format=%(refname)").splitlines()), "scans": results}
        (output / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(json.dumps(summary))
        return 1 if any(result["findings"] for result in results) else 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, StopIteration):
        print("Secret check failed to complete; verify Git, Gitleaks, download access and report-directory permissions.")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
