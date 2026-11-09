"""Instance gettext catalog for the launcher; user paths and protocol values stay literal."""
import builtins
import gettext
import json
import locale
import os
from pathlib import Path


class LauncherTranslations(gettext.NullTranslations):
    def __init__(self, language: str):
        super().__init__()
        self.messages = (json.loads(Path(__file__).with_name("launcher.en.json").read_text(encoding="utf-8"))
                         if language == "en" else {})

    def gettext(self, message: str) -> str:
        return self.messages.get(message, message)


def default_language() -> str:
    configured = os.environ.get("EVOWIKI_CLI_LANGUAGE")
    if configured in {"zh", "en"}:
        return configured
    return "zh" if (locale.getlocale()[0] or "en").lower().startswith("zh") else "en"


LANGUAGE = default_language()
TRANSLATIONS = LauncherTranslations(LANGUAGE)


def set_language(language: str) -> None:
    global LANGUAGE, TRANSLATIONS
    LANGUAGE = language
    TRANSLATIONS = LauncherTranslations(language)
    os.environ["EVOWIKI_CLI_LANGUAGE"] = language


def cli_text(value: str) -> str:
    return TRANSLATIONS.gettext(value)


def cli_format(message: str, **values) -> str:
    # Only catalogued literal labels/errors are translated. Path objects and unknown
    # user content are formatted as supplied.
    values = {key: cli_text(value) if isinstance(value, str) else
              cli_text(str(value)) if isinstance(value, Exception) else value
              for key, value in values.items()}
    return cli_text(message).format(**values)


def print(*values, **kwargs) -> None:
    builtins.print(*(cli_text(value) if isinstance(value, str) else value for value in values), **kwargs)
