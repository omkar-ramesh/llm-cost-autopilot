import pytest

from app.router import (
    fallback_chain,
    normalise_mode,
    route,
    score_complexity,
    tier_for_score,
    tier_of_model,
)

EASY = [{"role": "user", "content": "What is the capital of France?"}]

MEDIUM = [
    {
        "role": "user",
        "content": (
            "Here is our event payload: "
            + '{"type": "object", "properties": {"id": {"type": "string"}}}' * 6
            + " Evaluate the trade-offs of flattening this schema and explain why."
        ),
    }
]

HARD = [
    {"role": "system", "content": "You are a principal engineer. " * 40},
    {
        "role": "user",
        "content": "Refactor this module step by step.\n```python\n" + "x = 1\n" * 300 + "```",
    },
    {"role": "assistant", "content": "Sure, which part first?"},
    {"role": "user", "content": "First analyze the hot path, then design a fix and explain why."},
    {"role": "assistant", "content": "Understood."},
    {"role": "user", "content": "Also prove the complexity bound: $$O(n \\log n)$$"},
]


def test_scorer_is_pure():
    assert score_complexity(HARD) == score_complexity(HARD)


def test_empty_messages_score_zero():
    assert score_complexity([]) == 0.0


def test_scorer_bands():
    easy, medium, hard = score_complexity(EASY), score_complexity(MEDIUM), score_complexity(HARD)
    assert easy < medium < hard
    assert easy <= 0.35
    assert 0.35 < medium <= 0.70
    assert hard > 0.70


def test_score_is_clamped_to_unit_interval():
    huge = [{"role": "user", "content": "step by step analyze ```code``` " * 5000}]
    assert 0.0 <= score_complexity(huge) <= 1.0


def test_tools_raise_score():
    tools = [{"type": "function", "function": {"name": "get_weather"}}]
    assert score_complexity(EASY, tools=tools) > score_complexity(EASY)


@pytest.mark.parametrize(
    ("score", "expected"),
    [
        (0.0, "cheap"),
        (0.35, "cheap"),
        (0.36, "mid"),
        (0.70, "mid"),
        (0.71, "premium"),
        (1.0, "premium"),
    ],
)
def test_tier_for_score_bands(score, expected):
    assert tier_for_score(score) == expected


def test_easy_routes_cheap_hard_routes_premium():
    assert route(EASY).tier == "cheap"
    assert route(HARD).tier == "premium"


def test_mode_header_overrides_score():
    assert route(EASY, mode="quality").tier == "premium"
    assert route(HARD, mode="cheap").tier == "cheap"
    assert route(EASY, mode="balanced").tier == "cheap"


def test_unknown_mode_falls_back_to_balanced():
    assert normalise_mode("nonsense") == "balanced"
    assert normalise_mode(None) == "balanced"
    assert route(HARD, mode="nonsense").tier == "premium"


def test_model_override_pins_model():
    decision = route(HARD, model_override="gpt-4o-mini")
    assert decision.models == ["gpt-4o-mini"]
    assert decision.tier == "cheap"
    assert decision.override is True


def test_force_cheapest_beats_quality_mode():
    assert route(HARD, mode="quality", force_cheapest=True).tier == "cheap"


def test_fallback_chain_escalates_upward():
    chain = fallback_chain("cheap")
    assert chain[0] == "gpt-4o-mini"
    assert chain[-1] == "claude-opus-5"
    assert len(chain) == len(set(chain))
    assert fallback_chain("premium") == ["claude-opus-5"]


def test_tier_of_model():
    assert tier_of_model("gpt-4o") == "mid"
    assert tier_of_model("not-a-model") == "unknown"
