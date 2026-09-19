from app.pricing import cost_usd


def test_cost_matches_hand_calc():
    # gpt-4o-mini: $0.15/1M input, $0.60/1M output
    assert cost_usd("gpt-4o-mini", 1_000_000, 0) == 0.15
    assert cost_usd("gpt-4o-mini", 0, 1_000_000) == 0.60
    assert cost_usd("gpt-4o-mini", 1000, 500) == (1000 * 0.15 + 500 * 0.60) / 1_000_000


def test_unknown_model_is_free():
    assert cost_usd("some-unlisted-model", 1000, 1000) == 0.0
