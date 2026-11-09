"""The plugin's own decision code, run on what the server returned: plugin_next_step.ts is bundled
with the plugin's esbuild (obsidian-plugin/node_modules, `npm ci` there) and run with node. Fails
closed when node or the plugin's node_modules are missing: these checks are about what the learner
is offered, which the plugin decides."""

import functools
import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
PLUGIN = ROOT / "obsidian-plugin"
DRIVER = Path(__file__).with_name("plugin_next_step.ts")


@functools.cache
def _bundle() -> Path:
    node = shutil.which("node")
    esbuild = PLUGIN / "node_modules" / "esbuild" / "bin" / "esbuild"
    if node is None or not esbuild.exists():
        raise RuntimeError("the plugin checks need node and the plugin's packages: "
                           "run `npm ci` in obsidian-plugin")
    out = Path(tempfile.mkdtemp(prefix="evowiki-acceptance-")) / "next_step.mjs"
    subprocess.run([node, str(esbuild), str(DRIVER), "--bundle", "--platform=node",
                    "--format=esm", f"--outfile={out}", "--log-level=warning"],
                   check=True, cwd=PLUGIN)
    return out


def offered_next(catalog: dict, paths: list[dict], page: dict, progress: dict) -> dict[str, Any]:
    """{today: 今天's 下一步, end: the open page's 「读完了」 main button}, as the plugin renders them
    from what the server said (GET /wiki/progress, the page's next_step)."""
    r = subprocess.run([shutil.which("node") or "node", str(_bundle())],
                       input=json.dumps({"catalog": catalog, "paths": paths, "page": page,
                                         "progress": progress}),
                       capture_output=True, text=True, encoding="utf-8", check=False)
    if r.returncode != 0:
        raise RuntimeError(f"plugin_next_step failed: {r.stderr}")
    return json.loads(r.stdout)
