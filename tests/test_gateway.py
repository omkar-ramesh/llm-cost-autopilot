import json

from sqlmodel import select

from app import providers
from app.models import Request
from app.pricing import cost_usd

COMPLETION = {
    "id": "chatcmpl-1",
    "object": "chat.completion",
    "model": "gpt-4o-mini",
    "choices": [
        {"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}
    ],
    "usage": {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17},
}

PAYLOAD = {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hello"}]}


def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_missing_auth_rejected(client):
    resp = client.post("/v1/chat/completions", json=PAYLOAD, headers={"Authorization": ""})
    assert resp.status_code == 401


def test_completion_proxies_and_logs(client, session, monkeypatch):
    async def fake_acompletion(**kwargs):
        assert kwargs["model"] == "gpt-4o-mini"
        return COMPLETION

    monkeypatch.setattr(providers, "acompletion", fake_acompletion)

    resp = client.post("/v1/chat/completions", json=PAYLOAD)
    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"] == "hi"

    expected_cost = cost_usd("gpt-4o-mini", 12, 5)
    assert resp.headers["x-autopilot-model"] == "gpt-4o-mini"
    assert float(resp.headers["x-autopilot-cost-usd"]) == expected_cost
    assert float(resp.headers["x-autopilot-savings-usd"]) == 0.0

    row = session.exec(select(Request)).one()
    assert row.prompt_tokens == 12
    assert row.completion_tokens == 5
    assert row.cost_usd == expected_cost
    assert row.baseline_cost_usd == expected_cost
    assert row.status == "ok"


def test_streaming_yields_sse_and_logs(client, session, monkeypatch):
    chunks = [
        {"id": "1", "choices": [{"index": 0, "delta": {"content": "hi"}}]},
        {"id": "1", "choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 5}},
    ]

    async def fake_acompletion(**kwargs):
        assert kwargs["stream"] is True

        async def gen():
            for c in chunks:
                yield c

        return gen()

    monkeypatch.setattr(providers, "acompletion", fake_acompletion)

    resp = client.post("/v1/chat/completions", json={**PAYLOAD, "stream": True})
    assert resp.status_code == 200

    lines = [ln for ln in resp.text.split("\n\n") if ln.startswith("data: ")]
    assert lines[-1] == "data: [DONE]"
    assert json.loads(lines[0][6:])["choices"][0]["delta"]["content"] == "hi"

    row = session.exec(select(Request)).one()
    assert row.prompt_tokens == 12
    assert row.completion_tokens == 5


def test_upstream_error_logged(client, session, monkeypatch):
    async def fake_acompletion(**kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(providers, "acompletion", fake_acompletion)

    resp = client.post("/v1/chat/completions", json=PAYLOAD)
    assert resp.status_code == 502
    assert resp.json()["detail"]["error"]["type"] == "upstream_error"

    row = session.exec(select(Request)).one()
    assert row.status == "error"
