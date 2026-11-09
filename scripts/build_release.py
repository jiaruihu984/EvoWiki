"""Build version-matched Obsidian attachments and an archive with license notices."""

import argparse
import hashlib
import json
import shutil
import subprocess
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def dependency_notices(plugin: Path, npm: str) -> str:
    paths = subprocess.check_output([npm, "ls", "--omit=dev", "--all", "--parseable"],
                                    cwd=plugin, text=True).splitlines()
    notices = []
    for raw in paths:
        folder = Path(raw)
        if folder.resolve() == plugin.resolve():
            continue
        package = json.loads((folder / "package.json").read_text(encoding="utf-8"))
        licenses = sorted(p for p in folder.iterdir() if p.is_file() and
                          p.name.lower().split(".")[0] in {"license", "licence", "copying", "notice"})
        if not licenses:
            raise RuntimeError(f"Missing dependency license notice: {package['name']}")
        notices.append(f"\n=== {package['name']} {package['version']} ===\n")
        for path in licenses:
            notices.append(path.read_text(encoding="utf-8") + "\n")
    return "".join(notices)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / ".evowiki-run" / "release")
    args = parser.parse_args()
    plugin = ROOT / "obsidian-plugin"
    version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    manifest = json.loads((plugin / "manifest.json").read_text(encoding="utf-8"))
    package = json.loads((plugin / "package.json").read_text(encoding="utf-8"))
    if manifest["version"] != version or package["version"] != version:
        raise RuntimeError("Python, plugin manifest and npm versions must match")
    npm = shutil.which("npm")
    if npm is None:
        raise RuntimeError("Install Node.js and npm before building release files")
    subprocess.run([npm, "run", "build"], cwd=plugin, check=True)
    folder = args.output.resolve() / version
    folder.mkdir(parents=True, exist_ok=True)
    files = [plugin / name for name in ("main.js", "manifest.json", "styles.css")]
    files += [ROOT / name for name in ("LICENSE", "THIRD_PARTY_NOTICES.md")]
    notice = folder / "THIRD_PARTY_LICENSES.txt"
    notice.write_text(dependency_notices(plugin, npm), encoding="utf-8")
    files.append(notice)
    archive = folder / f"evowiki-{version}.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for source in files:
            if source.resolve() != (folder / source.name).resolve():
                shutil.copy2(source, folder / source.name)
            bundle.write(source, source.name)
    hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in [*files, archive]}
    (folder / "SHA256SUMS").write_text("".join(f"{digest}  {name}\n" for name, digest in hashes.items()), encoding="utf-8")
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    inputs = ["obsidian-plugin/src", "obsidian-plugin/package.json", "obsidian-plugin/package-lock.json",
              "obsidian-plugin/manifest.json", "obsidian-plugin/styles.css", "obsidian-plugin/esbuild.config.mjs",
              "pyproject.toml", "LICENSE", "THIRD_PARTY_NOTICES.md", "scripts/build_release.py"]
    dirty = bool(subprocess.check_output(["git", "status", "--porcelain", "--", *inputs], cwd=ROOT))
    (folder / "build.json").write_text(json.dumps({"version": version, "commit": commit,
        "source_dirty": dirty, "min_obsidian": manifest["minAppVersion"], "files": hashes}, indent=2), encoding="utf-8")
    print(f"Built {archive}; run product checks before publishing these files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
