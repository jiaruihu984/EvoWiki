"""Compile the transparent Windows tray application; never launch it or any service."""

import hashlib
import json
import os
import subprocess
from pathlib import Path


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    compiler = Path(os.environ.get("SystemRoot", "C:/Windows")) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    if not compiler.is_file():
        raise SystemExit("Windows .NET Framework x64 C# compiler is required to build EvoWiki.exe")
    source = root / "desktop/EvoWikiDesktop.cs"
    application_manifest = root / "desktop/app.manifest"
    output = root / "EvoWiki.exe"
    arguments = ["/nologo", "/noconfig", "/target:winexe", "/platform:anycpu", "/optimize+",
                 "/debug-", "/utf8output", "/codepage:65001", "/out:EvoWiki.exe",
                 "/win32manifest:desktop/app.manifest",
                 "/win32icon:desktop/icons/running.ico",
                 "/resource:desktop/icons/running.ico,EvoWiki.Running.ico",
                 "/resource:desktop/icons/stopped.ico,EvoWiki.Stopped.ico",
                 "/reference:System.dll", "/reference:System.Core.dll",
                 "/reference:System.Windows.Forms.dll", "/reference:System.Drawing.dll",
                 "/reference:System.Web.Extensions.dll", str(Path("desktop") / "EvoWikiDesktop.cs")]
    subprocess.run([str(compiler), *arguments], cwd=root, check=True)
    manifest = {"source": source.relative_to(root).as_posix(), "source_sha256": sha256(source),
                "application_manifest_sha256": sha256(application_manifest),
                "icons": {p.relative_to(root).as_posix(): sha256(p)
                          for p in sorted((root / "desktop/icons").iterdir()) if p.suffix in {".svg", ".ico"}},
                "compiler": "Windows .NET Framework v4.0.30319 csc.exe",
                "compiler_sha256": sha256(compiler), "arguments": arguments,
                "output": output.name, "output_sha256": sha256(output),
                "signed": False, "byte_reproducible": False,
                "note": "Legacy Framework csc has no deterministic switch; hashes identify this build only."}
    (root / "desktop/build-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": output.name, "sha256": manifest["output_sha256"], "launched": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
