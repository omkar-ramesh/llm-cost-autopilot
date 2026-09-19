import json
import re
from pathlib import Path

from app import providers
from app.pricing import cost_usd

DASHBOARD = Path(__file__).resolve().parent.parent / "config/grafana/dashboards/autopilot.json"

COMPLETION = {
    "id": "chatcmpl-1",
    "model": "gpt-4o-mini",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}}],
    "usage": {"prompt_tokens": 100, "completion_tokens": 50},
}

PAYLOAD = {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hello"}]}


def _sample(name: str, **labels) -> float:
    from app.metrics import REGISTRY

    return REGISTRY.get_sample_value(name, labels) or 0.0


def test_metrics_endpoint_exposes_counters(client, monkeypatch):
    async def fake_acompletion(**kwargs):
        return COMPLETION

    monkeypatch.setattr(providers, "acompletion", fake_acompletion)

    before_spend = _sample("autopilot_spend_usd_total", tenant_id="1", routed_model="gpt-4o-mini")
    before_reqs = _sample(
        "autopilot_requests_total",
        tenant_id="1",
        routed_model="gpt-4o-mini",
        tier="passthrough",
        status="ok",
    )

    for _ in range(20):
        assert client.post("/v1/chat/completions", json=PAYLOAD).status_code == 200

    after_spend = _sample("autopilot_spend_usd_total", tenant_id="1", routed_model="gpt-4o-mini")
    after_reqs = _sample(
        "autopilot_requests_total",
        tenant_id="1",
        routed_model="gpt-4o-mini",
        tier="passthrough",
        status="ok",
    )

    assert after_reqs - before_reqs == 20
    assert round(after_spend - before_spend, 12) == round(20 * cost_usd("gpt-4o-mini", 100, 50), 12)

    body = client.get("/metrics").text
    assert "autopilot_spend_usd_total" in body
    assert "autopilot_request_latency_seconds_bucket" in body
    assert "autopilot_tokens_total" in body


def test_tokens_counted_by_kind(client, monkeypatch):
    async def fake_acompletion(**kwargs):
        return COMPLETION

    monkeypatch.setattr(providers, "acompletion", fake_acompletion)

    before = _sample(
        "autopilot_tokens_total", tenant_id="1", routed_model="gpt-4o-mini", kind="prompt"
    )
    client.post("/v1/chat/completions", json=PAYLOAD)
    after = _sample(
        "autopilot_tokens_total", tenant_id="1", routed_model="gpt-4o-mini", kind="prompt"
    )
    assert after - before == 100


def test_dashboard_queries_reference_real_metrics():
    from app.metrics import REGISTRY

    # prometheus_client strips the `_total` / `_bucket` suffixes from metric.name
    exported = set()
    for metric in REGISTRY.collect():
        exported |= {metric.name, f"{metric.name}_total", f"{metric.name}_bucket"}

    dashboard = json.loads(DASHBOARD.read_text())
    referenced = set()
    for panel in dashboard["panels"]:
        for target in panel["targets"]:
            referenced |= set(re.findall(r"autopilot_[a-z_]+", target["expr"]))

    assert referenced
    assert referenced <= exported, referenced - exported
