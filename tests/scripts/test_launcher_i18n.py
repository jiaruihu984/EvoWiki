import re
from pathlib import Path

from evowiki_args import parse_launcher_args
from launcher_i18n import cli_format, cli_text, set_language


def test_cli_language_can_precede_or_follow_command():
    assert parse_launcher_args(["--language", "en", "up"], wrapper=True).language == "en"
    assert parse_launcher_args(["up", "--language", "zh"], wrapper=True).language == "zh"


def test_english_messages_translate_templates_and_labels_but_keep_chinese_paths():
    set_language("en")
    value = cli_format("{p0}：{p1}（可写）", p0="笔记库", p1=Path("D:/学习 vault"))
    assert value.startswith("Vault: D:") and "学习 vault" in value and value.endswith("(writable)")
    assert cli_text("请在应用中完成首次设置。学习模型尚未配置。") == "Complete Initial setup in the app. Learning model: not configured."


def test_chinese_messages_are_chinese_without_bilingual_suffix():
    set_language("zh")
    assert cli_text("请在应用中完成首次设置。学习模型尚未配置。") == "请在应用中完成首次设置。学习模型尚未配置。"


def test_help_english_has_no_chinese_and_needs_no_configuration(capsys):
    from evowiki_run import show_help
    assert show_help(Path("D:/isolated"), ["--language", "en", "--help"]) == 0
    text = capsys.readouterr().out
    assert "Commands, installation and recovery" in text
    assert not re.search(r"[\u3400-\u9fff]", text)


def test_doctor_english_uses_one_language_and_does_not_make_model_requests(tmp_path, monkeypatch, capsys):
    import evowiki_run as runner
    set_language("en")
    monkeypatch.setattr(runner.shutil, "which", lambda _: None)
    monkeypatch.setattr(runner, "git_runtime_dir", lambda: tmp_path / "Git-runtime")
    monkeypatch.setattr(runner, "missing_vc_runtime", list)
    def unavailable(*args, **kwargs):
        raise OSError("fixture")
    monkeypatch.setattr(runner.subprocess, "run", unavailable)
    assert runner.diagnose(tmp_path, {}) == 1
    output = capsys.readouterr().out
    assert "Complete Initial setup" in output or "complete Initial setup" in output
    assert not re.search(r"[\u3400-\u9fff]", output)
