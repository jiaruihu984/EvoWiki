"""The tmp git repo fixture of the forge tests, reused by the launcher's live tests (plan P3 T11)."""

import pytest

from tests.forge.patch.conftest import git_repo  # noqa: F401


@pytest.fixture(autouse=True)
def launcher_test_language(monkeypatch):
    """Existing Chinese expectations stay explicit; language tests select their own locale."""
    import launcher_i18n
    previous = launcher_i18n.LANGUAGE
    monkeypatch.setenv("EVOWIKI_CLI_LANGUAGE", "zh")
    launcher_i18n.set_language("zh")
    yield
    launcher_i18n.set_language(previous)
