from pathlib import Path

import pytest

from evowiki.forge.patch import importgraph


def _write(root: Path, files: dict[str, str]) -> None:
    for p, t in files.items():
        f = root / p
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(t, encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _write(tmp_path, {
        "src/evowiki/__init__.py": "",
        "src/evowiki/deep/__init__.py": "from . import x\n",
        "src/evowiki/deep/x.py": "from ..content import shared\n",
        "src/evowiki/deep/y.py": "from .x import thing\nimport evowiki.store.db as db\n",
        "src/evowiki/content/__init__.py": "",
        "src/evowiki/content/shared.py": "import json\nimport pydantic\n",
        "src/evowiki/store/__init__.py": "",
        "src/evowiki/store/db.py": "",
        "src/evowiki/dyn.py": "import importlib\nimportlib.import_module('evowiki.store.db')\n",
        "tests/__init__.py": "",
        "tests/deep/__init__.py": "",
        "tests/deep/test_y.py": "from evowiki.deep.y import thing\nfrom tests.deep import helper\n",
        "tests/deep/helper.py": "",
    })
    return tmp_path


def test_module_files(repo: Path):
    m = importgraph.module_files(repo)
    assert m["evowiki.deep.x"] == "src/evowiki/deep/x.py"
    assert m["evowiki.deep"] == "src/evowiki/deep/__init__.py"
    assert m["tests.deep.test_y"] == "tests/deep/test_y.py"


def test_relative_imports_resolve(repo: Path):
    g = importgraph.graph(repo)
    assert "src/evowiki/deep/x.py" in g["src/evowiki/deep/__init__.py"]           # from . import x
    assert "src/evowiki/content/shared.py" in g["src/evowiki/deep/x.py"]         # from ..content
    assert "src/evowiki/deep/x.py" in g["src/evowiki/deep/y.py"]                 # from .x import
    assert "src/evowiki/store/db.py" in g["src/evowiki/deep/y.py"]


def test_imports_include_parent_packages(repo: Path):
    g = importgraph.graph(repo)
    # importing evowiki.content.shared runs evowiki/__init__ and evowiki/content/__init__
    assert {"src/evowiki/__init__.py", "src/evowiki/content/__init__.py"} <= g["src/evowiki/deep/x.py"]


def test_constant_dynamic_import_is_an_edge(repo: Path):
    g = importgraph.graph(repo)
    assert "src/evowiki/store/db.py" in g["src/evowiki/dyn.py"]


def test_tests_in_graph_and_closure_and_importers(repo: Path):
    g = importgraph.graph(repo)
    assert "tests/deep/test_y.py" in g
    assert "tests/deep/helper.py" in g["tests/deep/test_y.py"]
    c = importgraph.closure(["tests/deep/test_y.py"], g)
    assert "src/evowiki/content/shared.py" in c and "tests/deep/test_y.py" in c
    r = importgraph.importers(["src/evowiki/content/shared.py"], g)
    assert {"src/evowiki/deep/x.py", "src/evowiki/deep/y.py", "tests/deep/test_y.py"} <= r
    assert "src/evowiki/store/db.py" not in r


def test_third_party_and_stdlib_ignored(repo: Path):
    g = importgraph.graph(repo)
    assert g["src/evowiki/content/shared.py"] <= {"src/evowiki/__init__.py",
                                                   "src/evowiki/content/__init__.py"}


@pytest.mark.parametrize("text", ["def (:\n", "x = '\0'\n"])
def test_unparseable_fails_closed(repo: Path, text: str):
    (repo / "src/evowiki/deep/bad.py").write_text(text, encoding="utf-8")
    with pytest.raises(importgraph.GraphError):
        importgraph.graph(repo)


def test_a_file_with_a_bom_parses_as_python_reads_it(repo: Path):
    (repo / "src" / "evowiki" / "bom.py").write_text("import evowiki.store.db\n", encoding="utf-8-sig")
    assert "src/evowiki/store/db.py" in importgraph.graph(repo)["src/evowiki/bom.py"]


def test_undecodable_fails_closed(repo: Path):
    (repo / "src/evowiki/deep/bad.py").write_bytes(b"x = '\xff\xfe'\n")
    with pytest.raises(importgraph.GraphError):
        importgraph.graph(repo)


def test_imports_function():
    s = importgraph.imports("src/evowiki/deep/a.py", "from .b import c\nimport os\n",
                            "evowiki.deep.a")
    assert {"evowiki.deep.b", "evowiki.deep.b.c", "os"} <= s
