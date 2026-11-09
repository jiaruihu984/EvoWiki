"""Argument encoding specific to uv's environment-file list parser."""

from pathlib import Path


def env_file_arg(path: Path) -> str:
    """Encode one path separately from OS argv quoting (astral-sh/uv#15815)."""
    return "".join(
        "\\" + char if char == "\\" or char.isspace() else char for char in path.as_posix()
    )
