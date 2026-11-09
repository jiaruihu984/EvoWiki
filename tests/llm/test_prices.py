import pytest

from evowiki.llm.prices import Price, PriceTable, load_prices


def test_cost_is_tokens_times_price_per_million():
    t = PriceTable({"gen:g-1": Price(1.0, 2.0)})
    assert t.cost("gen", "g-1", 1000, 200) == pytest.approx((1000 * 1.0 + 200 * 2.0) / 1e6)
    assert t.price("gen", "g-1") == Price(1.0, 2.0)


def test_unknown_model_has_no_cost():
    t = PriceTable({"gen:g-1": Price(1.0, 2.0)})
    assert t.cost("gen", "other", 10, 10) is None and t.price("ver", "g-1") is None


def test_default_file_loads_and_covers_runtime_models():
    t = load_prices()
    for key in ("anthropic:claude-sonnet-5-5", "anthropic:claude-haiku-4-5",
                "deepseek:deepseek-v4-pro", "deepseek:deepseek-v4-flash"):
        provider, model = key.split(":")
        assert t.price(provider, model) is not None, key
    # DeepSeek published peak rates (https://api-docs.deepseek.com/quick_start/pricing,
    # 2026-10-14, rechecked 2026-10-21): cache-miss input, output, cache-hit read; no cache write
    # rate; half of everything off-peak
    for provider in ("deepseek", "deepseek-nothink"):
        assert t.price(provider, "deepseek-v4-pro") == Price(1.32, 3.96, cache_read_per_mtok=0.044,
                                                             off_peak_factor=0.5)
        assert t.price(provider, "deepseek-v4-flash") == Price(0.3, 1.2, cache_read_per_mtok=0.006,
                                                               off_peak_factor=0.5)


CACHE_BASE = '[models."gen:g"]\ninput_per_mtok = 1\noutput_per_mtok = 1\n'


@pytest.mark.parametrize("body, match", [
    ('[models."gen:g"]\ninput_per_mtok = 1\n', "output_per_mtok"),
    ('[models."gen:g"]\ninput_per_mtok = 1\noutput_per_mtok = -1\n', "non-negative"),
    ('[models."gen:g"]\ninput_per_mtok = 1\noutput_per_mtok = nan\n', "non-negative"),
    ('[models."gen:g"]\ninput_per_mtok = true\noutput_per_mtok = 1\n', "number"),
    ('[models."gen:g"]\ninput_per_mtok = 1\noutput_per_mtok = 1\nextra = 1\n', "unknown"),
    ('[models."nocolon"]\ninput_per_mtok = 1\noutput_per_mtok = 1\n', "provider:model"),
    ('[models.":g"]\ninput_per_mtok = 1\noutput_per_mtok = 1\n', "provider:model"),
    ('[other]\nx = 1\n', "top-level"),
    (CACHE_BASE + 'cache_read_per_mtok = -0.1\n', "non-negative"),
    (CACHE_BASE + 'cache_write_per_mtok = "1"\n', "number"),
    (CACHE_BASE + 'cache_read_per_mtok = inf\n', "non-negative"),
    (CACHE_BASE + 'cache_write_1h_per_mtok = -2\n', "non-negative"),
    (CACHE_BASE + 'cache_read_per_mtok = -0.1\ncache_write_per_mtok = 1\n', "non-negative"),
    (CACHE_BASE + 'cache_read_per_mtok = 0.1\ncache_write_per_mtok = "1"\n', "number"),
    (CACHE_BASE + 'cache_read_per_mtok = 0.1\ncache_write_per_mtok = 1\ncache_hit_per_mtok = 1\n',
     "unknown"),
])
def test_bad_price_file_fails_closed(tmp_path, body, match):
    p = tmp_path / "prices.toml"
    p.write_text(body, encoding="utf-8")
    with pytest.raises(ValueError, match=match):
        load_prices(p)


ANTHROPIC_CACHE = {  # (input, output, cache read, 5-minute write, 1-hour write), USD / MTok
    "claude-opus-5-5": (4.0, 20.0, 0.20, 5.00, 8.00),
    "claude-sonnet-5-5": (2.0, 10.0, 0.20, 2.50, 4.00),
    "claude-haiku-4-5": (1.0, 5.0, 0.10, 1.25, 2.00),
}


@pytest.mark.parametrize("model", sorted(ANTHROPIC_CACHE))
def test_default_file_prices_cache_tokens_per_model(model):
    inp, out, read, write, write_1h = ANTHROPIC_CACHE[model]
    assert (write, write_1h) == (1.25 * inp, 2 * inp)    # Anthropic: 1.25x / 2x the input rate
    t = load_prices()
    assert t.price("anthropic", model) == Price(inp, out, read, write, write_1h)
    got = t.cost("anthropic", model, 1000, 500,
                 cache_read_tokens=50_000, cache_write_tokens=10_000)
    assert got == pytest.approx((1000 * inp + 500 * out + 50_000 * read + 10_000 * write) / 1e6)
    # backwards compatible: no cache tokens, the old two-rate formula
    assert t.cost("anthropic", model, 1000, 500) == pytest.approx((1000 * inp + 500 * out) / 1e6)
    # 4000 of the 10000 written tokens with the 1-hour TTL
    got = t.cost("anthropic", model, 1000, 500, 50_000, 10_000, 4_000)
    assert got == pytest.approx((1000 * inp + 500 * out + 50_000 * read + 6_000 * write
                                 + 4_000 * write_1h) / 1e6)


def test_the_provider_helper_is_the_price_table():
    from evowiki.llm.providers import anthropic_cost

    t = load_prices()
    for model in ANTHROPIC_CACHE:
        assert t.cost("anthropic", model, 1000, 500, 50_000, 10_000) == pytest.approx(
            anthropic_cost(model, 1000, 500, 50_000, 10_000)), model
        assert t.cost("anthropic", model, 1000, 500, 50_000, 10_000, 4_000) == pytest.approx(
            anthropic_cost(model, 1000, 500, 50_000, 10_000, 4_000)), model
    assert t.cost("anthropic", "claude-sonnet-5-5", 1000, 500, 50_000, 10_000) == pytest.approx(
        0.042)


def test_a_model_without_cache_rates_charges_cache_tokens_at_the_input_rate():
    t = PriceTable({"gen:g": Price(2.0, 4.0)})
    assert t.cost("gen", "g", 100, 10, 300, 200) == pytest.approx(
        ((100 + 300 + 200) * 2.0 + 10 * 4.0) / 1e6)
    # 1-hour writes without a rate: twice the input rate (Anthropic's 1-hour price, an upper bound)
    assert t.cost("gen", "g", 0, 0, 0, 200, 50) == pytest.approx((150 * 2.0 + 50 * 4.0) / 1e6)


@pytest.mark.parametrize(("body", "want"), [
    ('cache_read_per_mtok = 0.1\n', Price(1.0, 1.0, cache_read_per_mtok=0.1)),
    ('cache_write_per_mtok = 1.5\n', Price(1.0, 1.0, cache_write_per_mtok=1.5)),
])
def test_each_cache_rate_is_optional_on_its_own(tmp_path, body, want):
    p = tmp_path / "prices.toml"
    p.write_text(CACHE_BASE + body, encoding="utf-8")
    t = load_prices(p)
    assert t.price("gen", "g") == want
    # the missing rate falls back to the input rate (1.0)
    read = want.cache_read_per_mtok if want.cache_read_per_mtok is not None else 1.0
    write = want.cache_write_per_mtok if want.cache_write_per_mtok is not None else 1.0
    assert t.cost("gen", "g", 0, 0, 1_000_000, 1_000_000) == pytest.approx(read + write)


DEEPSEEK = {  # (input miss, output, cache hit), USD / MTok, peak
    "deepseek-v4-pro": (1.32, 3.96, 0.044),
    "deepseek-v4-flash": (0.3, 1.2, 0.006),
}


@pytest.mark.parametrize("provider", ["deepseek", "deepseek-nothink"])
@pytest.mark.parametrize("model", sorted(DEEPSEEK))
def test_deepseek_cache_hits_are_charged_at_the_hit_rate(provider, model):
    inp, out, hit = DEEPSEEK[model]
    t = load_prices()
    p = t.price(provider, model)
    assert p is not None and p.cache_write_per_mtok is None    # no write charge (0 tokens)
    # 60000 prompt tokens, 55000 of them cache hits, 1000 output tokens
    got = t.cost(provider, model, 5_000, 1_000, cache_read_tokens=55_000)
    assert got == pytest.approx((5_000 * inp + 1_000 * out + 55_000 * hit) / 1e6)
    assert got < t.cost(provider, model, 60_000, 1_000)        # cheaper than all-miss


def test_deepseek_pro_hit_example_in_dollars():
    t = load_prices()
    # 5000 * 1.32 + 1000 * 3.96 + 55000 * 0.044 = 6600 + 3960 + 2420 per million
    assert t.cost("deepseek", "deepseek-v4-pro", 5_000, 1_000, 55_000) == pytest.approx(0.01298)


def test_cache_rates_load_from_a_file(tmp_path):
    p = tmp_path / "prices.toml"
    p.write_text('[models."gen:g"]\ninput_per_mtok = 1\noutput_per_mtok = 2\n'
                 'cache_read_per_mtok = 0\ncache_write_per_mtok = 1.5\n', encoding="utf-8")
    t = load_prices(p)
    assert t.price("gen", "g") == Price(1.0, 2.0, 0.0, 1.5)
    assert t.cost("gen", "g", 0, 0, cache_read_tokens=1_000_000) == 0.0
    assert t.cost("gen", "g", 0, 0, cache_write_tokens=1_000_000) == pytest.approx(1.5)


def test_unknown_model_with_cache_tokens_has_no_cost():
    assert PriceTable({}).cost("anthropic", "x", 1, 1, 1, 1) is None
