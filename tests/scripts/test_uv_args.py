"""Exercise uv's own list parser; OS argument quoting alone cannot encode these paths."""

import os
import shutil
import subprocess
import sys

import pytest
from uv_args import env_file_arg


@pytest.mark.parametrize("through_cmd", [False, True])
def test_uv_reads_one_unicode_environment_file_with_spaces(tmp_path, through_cmd):
    if through_cmd and os.name != "nt":
        pytest.skip("Windows detached launch uses cmd.exe")
    uv = shutil.which("uv")
    if not uv:
        pytest.skip("uv is required to exercise its argument parser")
    directory = tmp_path / "installation 用户's folder\u3000space"
    directory.mkdir()
    env_file = directory / "test values.env"
    env_file.write_text("EVOWIKI_ENV_ARG_PROBE=verified\n", encoding="utf-8")
    env = {key: value for key, value in os.environ.items()
           if key not in {"UV_ENV_FILE", "UV_NO_ENV_FILE", "VIRTUAL_ENV"}}
    env["UV_CACHE_DIR"] = str(tmp_path / "uv-cache")
    args = [uv, "run", "--no-project", "--no-sync", "--no-python-downloads",
            "--python", sys.executable, "--env-file", env_file_arg(env_file), "python", "-c",
            "import os; print(os.environ['EVOWIKI_ENV_ARG_PROBE'])"]
    if through_cmd:
        script = tmp_path / "launch.cmd"
        script.write_text("@echo off\nchcp 65001 >nul\n" + subprocess.list2cmdline(args) + "\n",
                          encoding="utf-8")
        args = ["cmd.exe", "/d", "/c", str(script)]
    result = subprocess.run(args, env=env, capture_output=True, text=True, encoding="utf-8",
                            timeout=30, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "verified"
