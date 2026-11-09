import os
import shutil
import subprocess
import sys
from pathlib import Path


def test_patch_environment_file_belongs_to_the_shared_repo_root(tmp_path):
    from evowiki import paths
    from evowiki.forge.api import ForgeDeps, PatchDeps

    # Tool generation has no repository/env-file dependency.
    ForgeDeps(pool=None, converse=None, sandbox=None, trace=None)
    deps = PatchDeps(pool=None, converse=None, runner=None, trace=None, main_root=tmp_path)
    assert deps.env_file == paths.ROOT / ".env"


def test_paths_follow_a_different_checkout_and_its_worktrees(tmp_path):
    root = tmp_path / "my-wiki"
    package = root / "src" / "evowiki"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    source = Path(__file__).resolve().parents[1] / "src" / "evowiki" / "paths.py"
    shutil.copy2(source, package / "paths.py")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "src"], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                    "commit", "-qm", "initial"], check=True)
    live = tmp_path / "my-wiki-live"
    subprocess.run(["git", "-C", str(root), "worktree", "add", "-qb", "live", str(live)], check=True)
    for checkout in (root, live):
        env = {**os.environ, "PYTHONPATH": str(checkout / "src"), "PYTHONIOENCODING": "utf-8"}
        result = subprocess.run([sys.executable, "-c", ("from evowiki import paths; "
                                 "print(paths.ROOT); print(paths.LIVE_ROOT); print(paths.FORGE_ROOT); print(paths.WASI_ROOT)")],
                                cwd=tmp_path, env=env, text=True, capture_output=True, encoding="utf-8", check=True)
        assert result.stdout.splitlines() == [str(root), str(live), str(tmp_path / "my-wiki-forge"),
                                             str(root / ".forge/runtime/python-wasi")]
