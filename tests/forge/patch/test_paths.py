from pathlib import Path

import pytest

from evowiki.forge.patch import importgraph
from evowiki.forge.patch.paths import (
    Change,
    check_change,
    declared_distributions,
    env_secret_values,
    foreign_imports,
    secret_hits,
    simulator_files,
)

BASE = frozenset({"src/evowiki/deep/mathx.py", "tests/test_old.py",
                  "src/evowiki/content/shared.py"})


def test_allowed_edit_and_new_test_pass():
    assert check_change([Change("M", "src/evowiki/deep/mathx.py"),
                         Change("A", "tests/forge_fc1/test_add.py")],
                        base_files=BASE, sim_files=frozenset()) == ()


def test_denied_and_old_test_and_delete_and_rename():
    v = check_change([Change("M", "src/evowiki/store/db.py"),
                      Change("M", "tests/test_old.py"),
                      Change("D", "tests/test_old.py"),
                      Change("R", "src/evowiki/deep/new.py", old_path="src/evowiki/deep/mathx.py")],
                     base_files=BASE, sim_files=frozenset())
    assert any("store/db.py" in x for x in v)
    assert any("tests/test_old.py" in x for x in v)
    assert len(v) >= 3


def test_simulator_closure_denied(git_repo: Path):
    sim = simulator_files(git_repo)
    assert "src/evowiki/content/shared.py" in sim
    v = check_change([Change("M", "src/evowiki/content/shared.py")], base_files=BASE,
                     sim_files=sim)
    assert v and "模拟器" in v[0]


def test_symlink_and_type_change_refused():
    assert check_change([Change("A", "src/evowiki/deep/l.py", new_mode="120000")],
                        base_files=BASE, sim_files=frozenset())
    assert check_change([Change("T", "src/evowiki/deep/mathx.py")], base_files=BASE,
                        sim_files=frozenset())


def test_too_many_files_refused():
    many = [Change("A", f"tests/t/test_{i}.py") for i in range(13)]
    assert check_change(many, base_files=BASE, sim_files=frozenset())


def test_secret_hits_never_echo_value():
    diff = "+++ b/src/evowiki/deep/x.py\n+KEY = 'sk-abcdefghijklmnopqrstuvwxyz123456'\n+ok = 1\n"
    hits = secret_hits(diff, env_values=())
    assert hits and "sk-abc" not in " ".join(hits)
    assert secret_hits("+x = 'hello-literal-secret-value'\n",
                       env_values=("hello-literal-secret-value",))


def test_foreign_imports():
    texts = {"src/evowiki/deep/a.py": "import json\nimport requests\nfrom evowiki.deep import b\n"
                                       "import pydantic\n"}
    assert foreign_imports(texts, declared=frozenset({"pydantic"})) == ("requests",)


def test_unparseable_file_fails_closed(git_repo: Path):
    (git_repo / "src/evowiki/simulator/bad.py").write_text("def (:\n", encoding="utf-8")
    with pytest.raises(importgraph.GraphError):
        simulator_files(git_repo)


# --- beyond the plan's contract: the trust boundary's corners ----------------------------------

@pytest.mark.parametrize("path", [
    "src/evowiki/deep/../forge/x.py", "/src/evowiki/deep/a.py", "src\\evowiki\\deep\\a.py",
    "D:/Example/src/evowiki/deep/a.py", "./src/evowiki/deep/a.py", "src//evowiki/deep/a.py",
    "SRC/evowiki/deep/a.py", "src/evowiki/Forge/x.py", "src/evowiki/deep/a.py.",
    "src/evowiki/deep/a.py ", "src/evowiki/deep/a.py::$DATA", "src/evowiki/deep/SHARED~1.PY",
    "src/evowiki/deep/con.py", "src/evowiki/deep/NUL", "src/evowiki/deep/.git/config",
    "src/evowiki/deep/.gitattributes", "src/evowiki/deep/a\x00.py", "src/evowiki/deep/a\n.py",
    "src/evowiki/deep/", ""])
def test_unsafe_paths_refused(path):
    assert check_change([Change("A", path)], base_files=BASE, sim_files=frozenset())


def test_blocklist_wins_over_allowlist():
    # forge/** and store/** are denied; a denied file never passes, new or existing
    for p in ("src/evowiki/forge/policy.py", "src/evowiki/forge/patch/paths.py",
              "src/evowiki/store/db.py", "pyproject.toml", ".env"):
        assert check_change([Change("A", p)], base_files=BASE, sim_files=frozenset())
        assert check_change([Change("M", p)], base_files=BASE | {p}, sim_files=frozenset())


def test_case_variant_of_existing_test_is_an_existing_test():
    # on Windows tests/Test_old.py IS tests/test_old.py: it must count as modifying an old test
    assert check_change([Change("A", "tests/Test_old.py")], base_files=BASE,
                        sim_files=frozenset())
    assert check_change([Change("M", "TESTS/test_old.py")], base_files=BASE,
                        sim_files=frozenset())


def test_case_variant_of_simulator_file_is_refused():
    sim = frozenset({"src/evowiki/content/shared.py"})
    v = check_change([Change("A", "src/evowiki/content/Shared.py")], base_files=BASE,
                     sim_files=sim)
    assert v


def test_case_collision_with_existing_file_refused():
    assert check_change([Change("A", "src/evowiki/deep/MathX.py")], base_files=BASE,
                        sim_files=frozenset())


def test_rename_into_allowed_from_denied_refused():
    v = check_change([Change("R", "src/evowiki/deep/x.py", old_path="src/evowiki/store/db.py")],
                     base_files=BASE | {"src/evowiki/store/db.py"}, sim_files=frozenset())
    assert any("store/db.py" in x for x in v)


def test_copy_of_simulator_file_is_fine_but_target_checked():
    # C = new file whose content came from another; the source is unchanged
    assert check_change([Change("C", "src/evowiki/deep/copy.py",
                                old_path="src/evowiki/content/shared.py")],
                        base_files=BASE, sim_files=frozenset({"src/evowiki/content/shared.py"})) == ()
    assert check_change([Change("C", "src/evowiki/forge/copy.py",
                                old_path="src/evowiki/deep/mathx.py")],
                        base_files=BASE, sim_files=frozenset())


@pytest.mark.parametrize("status,mode", [("U", "100644"), ("X", "100644"), ("", "100644"),
                                         ("A", "160000"), ("A", "040000"), ("M", "")])
def test_unknown_status_or_mode_refused(status, mode):
    assert check_change([Change(status, "src/evowiki/deep/z.py", new_mode=mode)],
                        base_files=BASE, sim_files=frozenset())


def test_executable_mode_allowed():
    assert check_change([Change("A", "src/evowiki/deep/z.py", new_mode="100755")],
                        base_files=BASE, sim_files=frozenset()) == ()


def test_messages_are_deduplicated_and_tuple():
    v = check_change([Change("M", "src/evowiki/store/db.py")] * 2, base_files=BASE,
                     sim_files=frozenset())
    assert isinstance(v, tuple) and len(v) == len(set(v))


@pytest.mark.parametrize("line", [
    "+k = 'sk-ant-api03-abcdefghijklmnopqrstuvwx'",
    "+t = 'tvly-ABCDEFGHIJKLMNOPQRS'",
    "+a = 'AKIAABCDEFGHIJKLMNOP'",
    "+-----BEGIN RSA PRIVATE KEY-----",
    "+api_key = 'abcdefghijklmnop'",
    "+PASSWORD: \"correct horse battery\"",
])
def test_secret_patterns(line):
    hits = secret_hits(line + "\n", env_values=())
    assert hits and all("第 1 行" in h for h in hits)


def test_secret_scan_ignores_removed_and_context_lines():
    diff = "-k = 'sk-abcdefghijklmnopqrstuvwxyz123456'\n k2 = 'sk-abcdefghijklmnopqrstuvwxyz0'\n"
    assert secret_hits(diff, env_values=()) == ()


def test_secret_scan_covers_plus_plus_lines():
    # a content line beginning with "++" shows up as "+++..." in the diff: still scanned
    assert secret_hits("+++ b/sk-abcdefghijklmnopqrstuvwxyz123456\n", env_values=())


def test_env_values_are_never_echoed():
    hits = secret_hits("+x = 'zz-my-private-value-123'\n", env_values=("zz-my-private-value-123",))
    assert hits and "zz-my" not in " ".join(hits)


def test_env_secret_values(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text("# c\nDEEPSEEK_API_KEY=sk-0123456789abcdef\nexport B=\"quoted-value-long\"\n"
                   "SHORT=abc\nEMPTY=\n", encoding="utf-8")
    vals = env_secret_values(env)
    assert set(vals) == {"sk-0123456789abcdef", "quoted-value-long"}
    assert env_secret_values(tmp_path / "missing.env") == ()


def test_foreign_imports_more_forms():
    texts = {
        "src/evowiki/deep/a.py": "from . import x\nfrom .y import z\nimport os.path\n"
                                 "import tests.forge\nfrom yaml import safe_load\n"
                                 "import importlib\nimportlib.import_module('httpx')\n",
        "src/evowiki/deep/README.md": "import requests\n",
    }
    assert foreign_imports(texts, declared=frozenset()) == ("httpx", "yaml")


def test_foreign_imports_unparseable_fails_closed():
    with pytest.raises(importgraph.GraphError):
        foreign_imports({"src/evowiki/deep/a.py": "def (:\n"}, declared=frozenset())


def test_declared_distributions(tmp_path: Path):
    pp = tmp_path / "pyproject.toml"
    pp.write_text(
        "[project]\nname='x'\ndependencies=['pydantic>=2', 'PyYAML[extra]>=6; python_version>\"3\"']\n"
        "[project.optional-dependencies]\nweb=['fastapi']\n"
        "[dependency-groups]\ndev=['pytest>=8', {include-group='web'}]\n", encoding="utf-8")
    d = declared_distributions(pp)
    assert {"pydantic", "yaml", "fastapi", "pytest"} <= d
    assert "requests" not in d


def test_simulator_files_needs_a_simulator(tmp_path: Path):
    (tmp_path / "src/evowiki/deep").mkdir(parents=True)
    (tmp_path / "src/evowiki/deep/a.py").write_text("X = 1\n", encoding="utf-8")
    with pytest.raises(importgraph.GraphError):
        simulator_files(tmp_path)


def test_simulator_closure_includes_packages_and_transitive(git_repo: Path):
    (git_repo / "src/evowiki/content/shared.py").write_text("from evowiki.deep import mathx\n",
                                                            encoding="utf-8")
    sim = simulator_files(git_repo)
    assert {"src/evowiki/simulator/run.py", "src/evowiki/content/shared.py",
            "src/evowiki/content/__init__.py", "src/evowiki/__init__.py",
            "src/evowiki/deep/mathx.py"} <= sim
    assert "src/evowiki/store/db.py" not in sim


def test_real_repo_simulator_closure_is_computable():
    root = Path(__file__).resolve().parents[3]
    sim = simulator_files(root)
    assert "src/evowiki/simulator/driver.py" in sim
    assert all(not p.startswith("tests/") for p in sim)
