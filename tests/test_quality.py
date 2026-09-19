import json

import pytest
from sqlmodel import select

from app import providers, quality
from app.models import Eval, Request

EASY = {"model": "claude-opus-5", "messages": [{"role": "user", "content": "hi there"}]}


def _reply(text: str, model: str = "m") -> dict:
    return {
        "id": "x",
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


@pytest.fixture
def always_sample(monkeypatch):
    monkeypatch.setattr(quality, "should_sample", lambda: True)


@pytest.fixture
def never_sample(monkeypatch):
    monkeypatch.setattr(quality, "should_sample", lambda: False)


def judge_stub(monkeypatch, score: float):
    """Routed call returns an answer; baseline and judge calls are stubbed."""
    judge_model = "gpt-4o"

    async def fake_acompletion(**kwargs):
        if kwargs["model"] == judge_model and "REFERENCE ANSWER" in str(kwargs["messages"]):
            return _reply(json.dumps({"score": score, "reason": "stub"}), judge_model)
        return _reply("an answer", kwargs["model"])

    monkeypatch.setattr(providers, "acompletion", fake_acompletion)


# --- prompt_class ------------------------------------------------------------


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("hello there", "plain:short"),
        ("```python\nx=1\n```", "code:short"),
        ("prove the theorem", "math:short"),
        ("respond only in valid json", "schema:short"),
        ("analyze the tradeoffs", "multistep:short"),
    ],
)
def test_prompt_class_buckets(content, expected):
    assert quality.prompt_class([{"role": "user", "content": content}]) == expected


def test_prompt_class_uses_length_bucket():
    long_text = "hello world " * 300
    assert quality.prompt_class([{"role": "user", "content": long_text}]) == "plain:long"


def test_tools_take_priority():
    tools = [{"type": "function", "function": {"name": "f"}}]
    assert quality.prompt_class([{"role": "user", "content": "hi"}], tools) == "tools:short"


# --- judge parsing -----------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('{"score": 0.9, "reason": "good"}', 0.9),
        ('```json\n{"score": 0.4, "reason": "meh"}\n```', 0.4),
        ("the score is 0.75 overall", 0.75),
        ('{"score": 5}', 1.0),
        ('{"score": -2}', 0.0),
        ("no number here", 0.0),
    ],
)
def test_parse_judge_score(raw, expected):
    assert quality.parse_judge_score(raw)[0] == expected


# --- escalation --------------------------------------------------------------


def test_escalates_after_three_consecutive_fails():
    assert not quality.is_escalated("code:short")
    assert quality.record_outcome("code:short", False) is False
    assert quality.record_outcome("code:short", False) is False
    assert quality.record_outcome("code:short", False) is True
    assert quality.is_escalated("code:short")


def test_pass_resets_the_fail_streak():
    quality.record_outcome("code:short", False)
    quality.record_outcome("code:short", False)
    quality.record_outcome("code:short", True)
    assert quality.record_outcome("code:short", False) is False
    assert not quality.is_escalated("code:short")


def test_escalation_is_per_class():
    for _ in range(3):
        quality.record_outcome("code:short", False)
    assert quality.is_escalated("code:short")
    assert not quality.is_escalated("plain:short")


def test_is_escalated_survives_redis_outage(monkeypatch):
    def boom():
        raise ConnectionError("redis down")

    monkeypatch.setattr(quality, "get_redis", boom)
    assert quality.is_escalated("code:short") is False


# --- shadow eval through the pipeline ---------------------------------------


def test_sampled_routed_down_request_is_evaluated(client, session, monkeypatch, always_sample):
    judge_stub(monkeypatch, 0.95)
    assert client.post("/v1/chat/completions", json=EASY).status_code == 200

    row = session.exec(select(Eval)).one()
    assert row.score == 0.95
    assert row.passed is True
    assert row.judge_model == "gpt-4o"
    assert row.request_id == session.exec(select(Request)).one().id


def test_unsampled_request_is_not_evaluated(client, session, monkeypatch, never_sample):
    judge_stub(monkeypatch, 0.95)
    client.post("/v1/chat/completions", json=EASY)
    assert session.exec(select(Eval)).all() == []


def test_request_not_routed_down_is_not_evaluated(client, session, monkeypatch, always_sample):
    judge_stub(monkeypatch, 0.95)
    payload = {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi there"}]}
    client.post("/v1/chat/completions", json=payload)
    assert session.exec(select(Eval)).all() == []


def test_failing_judge_escalates_tier_after_threshold(client, session, monkeypatch, always_sample):
    judge_stub(monkeypatch, 0.2)

    for _ in range(3):
        resp = client.post("/v1/chat/completions", json=EASY)
        assert resp.headers["x-autopilot-tier"] == "cheap"

    assert quality.is_escalated("plain:short")

    escalated = client.post("/v1/chat/completions", json=EASY)
    assert escalated.headers["x-autopilot-tier"] == "mid"

    evals = session.exec(select(Eval)).all()
    assert len(evals) >= 3
    assert all(e.passed is False for e in evals[:3])


def test_escalation_does_not_override_cheap_mode(client, monkeypatch, always_sample):
    judge_stub(monkeypatch, 0.2)
    for _ in range(3):
        client.post("/v1/chat/completions", json=EASY)

    resp = client.post(
        "/v1/chat/completions", json=EASY, headers={"x-autopilot-mode": "cheap"}
    )
    assert resp.headers["x-autopilot-tier"] == "cheap"


def test_shadow_eval_error_does_not_break_request(client, session, monkeypatch, always_sample):
    calls = {"n": 0}

    async def flaky(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return _reply("an answer", kwargs["model"])
        raise RuntimeError("judge unavailable")

    monkeypatch.setattr(providers, "acompletion", flaky)

    assert client.post("/v1/chat/completions", json=EASY).status_code == 200
    assert session.exec(select(Eval)).all() == []


# --- quality endpoint --------------------------------------------------------


def test_quality_endpoint_reports_pass_rate_per_tier(client, monkeypatch, always_sample):
    judge_stub(monkeypatch, 0.95)
    client.post("/v1/chat/completions", json=EASY)

    judge_stub(monkeypatch, 0.1)
    client.post("/v1/chat/completions", json=EASY)

    report = client.get("/v1/autopilot/quality").json()
    cheap = report["tiers"]["cheap"]

    assert cheap["evals"] == 2
    assert cheap["passed"] == 1
    assert cheap["pass_rate"] == 0.5
    assert cheap["avg_score"] == pytest.approx(0.525)
    assert report["overall"]["pass_rate"] == 0.5
    assert report["pass_threshold"] == 0.8


def test_quality_endpoint_empty_state(client):
    report = client.get("/v1/autopilot/quality").json()
    assert report["tiers"] == {}
    assert report["overall"]["pass_rate"] is None


def test_quality_endpoint_requires_auth(client):
    resp = client.get("/v1/autopilot/quality", headers={"Authorization": ""})
    assert resp.status_code == 401
