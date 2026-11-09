import re
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def test_pydantic_floor_covers_field_exclude_if():
    # Field(exclude_if=...) (pipeline/report.py) first shipped in pydantic 2.12.0.
    deps = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    [spec] = [d for d in deps["project"]["dependencies"] if d.startswith("pydantic")]
    floor = re.fullmatch(r"pydantic>=(\d+)\.(\d+)(?:\.\d+)?", spec)
    assert floor is not None and (int(floor[1]), int(floor[2])) >= (2, 12), spec
