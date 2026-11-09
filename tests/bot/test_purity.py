import subprocess
import sys

import pytest


@pytest.mark.parametrize("module", ["rules", "gate", "suppression", "schedule"])
def test_pure_modules_do_not_load_psycopg(module):
    code = (f"import sys, evowiki.bot.{module}; "
            "sys.exit(1 if 'psycopg' in sys.modules else 0)")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stderr or "psycopg was imported"
