"""The live harness's spend report (tests/llm_cases/live/harness.py `Live.spend` / `Live.report`),
offline: meter rows in, calls / cost / token counts per purpose and in total out, including the
prompt-cache reads (DeepSeek context-cache hits) and the hit rate. No gateway, no network; the
report goes to a temporary directory."""

import json

import pytest

from evowiki.llm.usage import Usage
from tests.llm_cases.live import harness
from tests.llm_cases.live.harness import Live, MemoryMeter


def _u(inp, read, out, cost, write=0):
    return Usage(provider="deepseek", model="deepseek-v4-pro", input_tokens=inp,
                 output_tokens=out, cache_read_tokens=read, cache_write_tokens=write,
                 cost_usd=cost)


def _live():
    meter = MemoryMeter()
    meter.rows += [("deep.write", _u(5_000, 55_000, 1_000, 0.01298), False),
                   ("deep.write", _u(6_000, 54_000, 2_000, 0.02), False),
                   ("deep.plan", _u(10_000, 0, 500, None), False),
                   ("deep.write", _u(9_999, 9_999, 9_999, 9.0), True)]      # replayed: not paid
    return Live(name="unit", gateway=None, meter=meter)


def test_spend_adds_token_counts_per_purpose_and_in_total():
    out = _live().spend()
    w, p = out["by_purpose"]["deep.write"], out["by_purpose"]["deep.plan"]
    assert (w["calls"], w["input_tokens"], w["cache_read_tokens"], w["cache_write_tokens"],
            w["output_tokens"]) == (2, 11_000, 109_000, 0, 3_000)
    assert w["cost_usd"] == pytest.approx(0.03298) and w["unpriced"] == 0
    assert w["cache_hit_rate"] == pytest.approx(round(109_000 / 120_000, 4))
    assert (p["calls"], p["unpriced"], p["cache_hit_rate"]) == (1, 1, 0.0)
    assert (out["calls"], out["input_tokens"], out["cache_read_tokens"], out["output_tokens"]) == (
        3, 21_000, 109_000, 3_500)
    assert out["cache_hit_rate"] == pytest.approx(round(109_000 / 130_000, 4))
    assert out["cost_usd"] == pytest.approx(0.03298)


def test_spend_without_prompt_tokens_has_no_hit_rate():
    live = Live(name="unit", gateway=None, meter=MemoryMeter())
    out = live.spend()
    assert out["calls"] == 0 and out["cache_hit_rate"] is None and out["input_tokens"] == 0


def test_report_writes_the_token_counts(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(harness, "RESULTS", tmp_path)
    out = _live().report({"extra": 1})
    saved = json.loads((tmp_path / "unit.json").read_text(encoding="utf-8"))
    assert saved["cache_read_tokens"] == 109_000 and saved["extra"] == 1
    assert saved["by_purpose"]["deep.write"]["cache_read_tokens"] == 109_000
    assert out["input_tokens"] == saved["input_tokens"] == 21_000
    assert "cache read 109000" in capsys.readouterr().out
