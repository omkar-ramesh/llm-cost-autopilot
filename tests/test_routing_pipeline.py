import pytest
from sqlmodel import select

from app import gateway, providers
from app.models import Request
from app.pricing import cost_usd

EASY = {"model": "claude-opus-5", "messages": [{"role": "user", "content": "hi there"}]}
HARD = {
    "model": "claude-opus-5",
    "messages": [
        {"role": "system", "content": "You are a principal engineer. " * 40},
        {
            "role": "user",
            "content": "Refactor step by step.\n```python\n" + "x = 1\n" * 300 + "```",
        },
        {"role": "assistant", "content": "Which part?"},
        {"role": "user", "content": "First analyze the hot path, then design a fix, explain why."},
        {"role": "assistant", "content": "Understood."},
        {"role": "user", "content": "Also prove the bound: $$O(n \\log n)$$"},
    ],
}


class FakeStatusError(Exception):
    def __init__(self, status_code: int):
        super().__init__(f"status {status_code}")
        self.status_code = status_code


class FakeTimeoutError(Exception):
    pass


def _completion(model: str) -> dict:
    return {
        "id": "chatcmpl-1",
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50},
    }


def _record_model(monkeypatch, fail_for: dict[str, Exception] | None = None) -> list[str]:
    seen: list[str] = []
    fail_for = fail_for or {}

    async def fake_acompletion(**kwargs):
        model = kwargs["model"]
        seen.append(model)
        if model in fail_for:
            raise fail_for[model]
        return _completion(model)

    monkeypatch.setattr(providers, "acompletion", fake_acompletion)
    return seen


def test_easy_prompt_routes_to_cheap_tier(client, session, monkeypatch):
    seen = _record_model(monkeypatch)
    resp = client.post("/v1/chat/completions", json=EASY)

    assert resp.headers["x-autopilot-tier"] == "cheap"
    assert resp.headers["x-autopilot-model"] == "gpt-4o-mini"
    assert seen == ["gpt-4o-mini"]

    row = session.exec(select(Request)).one()
    assert row.requested_model == "claude-opus-5"
    assert row.routed_model == "gpt-4o-mini"
    assert row.tier == "cheap"


def test_hard_prompt_routes_to_premium(client, monkeypatch):
    _record_model(monkeypatch)
    resp = client.post("/v1/chat/completions", json=HARD)
    assert resp.headers["x-autopilot-tier"] == "premium"
    assert resp.headers["x-autopilot-model"] == "claude-opus-5"


def test_savings_recorded_when_routed_down(client, session, monkeypatch):
    _record_model(monkeypatch)
    resp = client.post("/v1/chat/completions", json=EASY)

    row = session.exec(select(Request)).one()
    assert row.cost_usd == cost_usd("gpt-4o-mini", 100, 50)
    assert row.baseline_cost_usd == cost_usd("claude-opus-5", 100, 50)
    assert row.baseline_cost_usd > row.cost_usd

    savings = row.baseline_cost_usd - row.cost_usd
    assert float(resp.headers["x-autopilot-savings-usd"]) == pytest.approx(savings)


def test_complexity_score_persisted(client, session, monkeypatch):
    _record_model(monkeypatch)
    client.post("/v1/chat/completions", json=EASY)
    row = session.exec(select(Request)).one()
    assert 0.0 <= row.complexity_score <= 0.35


def test_mode_header_forces_quality(client, monkeypatch):
    seen = _record_model(monkeypatch)
    resp = client.post(
        "/v1/chat/completions", json=EASY, headers={"x-autopilot-mode": "quality"}
    )
    assert resp.headers["x-autopilot-tier"] == "premium"
    assert seen == ["claude-opus-5"]


def test_model_override_header_pins_model(client, monkeypatch):
    seen = _record_model(monkeypatch)
    resp = client.post(
        "/v1/chat/completions", json=HARD, headers={"x-autopilot-model": "gpt-4o-mini"}
    )
    assert seen == ["gpt-4o-mini"]
    assert resp.headers["x-autopilot-model"] == "gpt-4o-mini"


@pytest.mark.parametrize("exc", [FakeStatusError(429), FakeStatusError(503), FakeTimeoutError()])
def test_fallback_advances_on_retryable_error(client, monkeypatch, exc):
    seen = _record_model(monkeypatch, fail_for={"gpt-4o-mini": exc})
    resp = client.post("/v1/chat/completions", json=EASY)

    assert resp.status_code == 200
    assert seen == ["gpt-4o-mini", "claude-haiku-4-5"]
    assert resp.headers["x-autopilot-model"] == "claude-haiku-4-5"


def test_non_retryable_error_does_not_fall_back(client, monkeypatch):
    seen = _record_model(monkeypatch, fail_for={"gpt-4o-mini": FakeStatusError(400)})
    resp = client.post("/v1/chat/completions", json=EASY)

    assert resp.status_code == 502
    assert seen == ["gpt-4o-mini"]


def test_fallback_exhausts_whole_chain(client, session, monkeypatch):
    chain_error = {
        m: FakeStatusError(500)
        for m in ["gpt-4o-mini", "claude-haiku-4-5", "gpt-4o", "claude-sonnet-5", "claude-opus-5"]
    }
    seen = _record_model(monkeypatch, fail_for=chain_error)
    resp = client.post("/v1/chat/completions", json=EASY)

    assert resp.status_code == 502
    assert len(seen) == 5
    assert session.exec(select(Request)).one().status == "error"


def test_is_retryable_matches_config():
    assert gateway.is_retryable(FakeStatusError(429))
    assert gateway.is_retryable(FakeStatusError(500))
    assert gateway.is_retryable(FakeTimeoutError())
    assert not gateway.is_retryable(FakeStatusError(400))
    assert not gateway.is_retryable(ValueError("nope"))


def test_streaming_routes_and_logs(client, session, monkeypatch):
    async def fake_acompletion(**kwargs):
        assert kwargs["model"] == "gpt-4o-mini"

        async def gen():
            yield {"id": "1", "choices": [{"index": 0, "delta": {"content": "ok"}}]}
            yield {
                "id": "1",
                "choices": [],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50},
            }

        return gen()

    monkeypatch.setattr(providers, "acompletion", fake_acompletion)
    resp = client.post("/v1/chat/completions", json={**EASY, "stream": True})

    assert resp.headers["x-autopilot-tier"] == "cheap"
    row = session.exec(select(Request)).one()
    assert row.routed_model == "gpt-4o-mini"
    assert row.baseline_cost_usd > row.cost_usd
