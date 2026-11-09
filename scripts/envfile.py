"""The one reader of a .env file for the scripts: python-dotenv's parser, so
`export KEY=v`, inline `# comments`, single and double quotes (and their escapes) mean the same in
every script. Values are literal: no `${VAR}` interpolation. Scripts import it as a sibling
(`from envfile import read_env_file`); scripts in a subdirectory put scripts/ on sys.path first."""

from io import StringIO
from pathlib import Path

from dotenv import dotenv_values


def read_env_file(path: Path, *, missing_ok: bool = False) -> dict[str, str]:
    """KEY -> value of the file at `path`. A key written without `=` is skipped. A missing or
    unreadable file raises OSError, or gives {} with missing_ok=True."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        if missing_ok:
            return {}
        raise
    return {k: v for k, v in dotenv_values(stream=StringIO(text), interpolate=False).items()
            if v is not None}
