from datetime import UTC, datetime

import pytest
from sqlmodel import select

from app import budget, providers
from app.auth import Principal
from app.models import BudgetEvent

PAYLOAD = {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi there"}]}


def _principal(daily=1.0, monthly=10.0) -> Principal:
    return Principal(tenant_id=1, key_id=1, daily_budget_usd=daily, monthly_budget_usd=monthly)


@pytest.fixture
def stub_provider(monkeypatch):
    async def fake_acompletion(**kwargs):
        return {
            "id": "x",
            "model": kwargs["model"],
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 1000},
        }

    monkeypatch.setattr(providers, "acompletion", fake_acompletion)


# --- counters ----------------------------------------------------------------


def test_spend_starts_at_zero():
    assert budget.get_spend(1) == (0.0, 0.0)


def test_record_spend_accumulates_both_windows():
    budget.record_spend(1, 0.25)
    budget.record_spend(1, 0.25)
    daily, monthly = budget.get_spend(1)
    assert daily == pytest.approx(0.5)
    assert monthly == pytest.approx(0.5)


def test_record_spend_ignores_zero_and_negative():
    budget.record_spend(1, 0.0)
    budget.record_spend(1, -5.0)
    assert budget.get_spend(1) == (0.0, 0.0)


def test_spend_is_per_tenant():
    budget.record_spend(1, 0.5)
    assert budget.get_spend(2) == (0.0, 0.0)


def test_counters_are_scoped_to_their_window():
    jan = datetime(2026, 1, 15, tzinfo=UTC)
    feb = datetime(2026, 2, 15, tzinfo=UTC)
    assert budget.spend_key(1, budget.DAILY, jan) != budget.spend_key(1, budget.DAILY, feb)
    assert budget.spend_key(1, budget.MONTHLY, jan) != budget.spend_key(1, budget.MONTHLY, feb)
    assert budget.window_id(budget.MONTHLY, jan) == "2026-01"


# --- cap states --------------------------------------------------------------


def test_under_cap_is_ok():
    budget.record_spend(1, 0.5)
    assert budget.check(_principal()).state == budget.OK


def test_soft_cap_at_eighty_percent():
    budget.record_spend(1, 0.8)
    status = budget.check(_principal())
    assert status.state == budget.SOFT
    assert status.breached_period == budget.DAILY


def test_hard_cap_at_full_budget():
    budget.record_spend(1, 1.0)
    status = budget.check(_principal())
    assert status.state == budget.HARD
    assert status.over_hard_cap


def test_monthly_cap_breaches_independently():
    budget.record_spend(1, 8.5)
    status = budget.check(_principal(daily=100.0, monthly=10.0))
    assert status.state == budget.SOFT
    assert status.breached_period == budget.MONTHLY


def test_hard_cap_wins_over_soft_cap():
    budget.record_spend(1, 10.0)
    status = budget.check(_principal(daily=100.0, monthly=10.0))
    assert status.state == budget.HARD
    assert status.breached_period == budget.MONTHLY


def test_zero_budget_disables_that_period():
    budget.record_spend(1, 50.0)
    assert budget.check(_principal(daily=0.0, monthly=0.0)).state == budget.OK


def test_hard_cap_error_json_is_actionable():
    budget.record_spend(1, 1.0)
    err = budget.check(_principal()).as_error()["error"]
    assert err["type"] == "budget_exceeded"
    assert err["code"] == "daily_hard_cap"
    assert err["daily_budget_usd"] == 1.0
    assert "budget exhausted" in err["message"]


# --- alert dedup -------------------------------------------------------------


def test_alert_claimed_once_per_window():
    assert budget.claim_alert(1, budget.SOFT, budget.DAILY) is True
    assert budget.claim_alert(1, budget.SOFT, budget.DAILY) is False


def test_soft_and_hard_alerts_are_claimed_separately():
    assert budget.claim_alert(1, budget.SOFT, budget.DAILY) is True
    assert budget.claim_alert(1, budget.HARD, budget.DAILY) is True


def test_alerts_are_per_tenant():
    assert budget.claim_alert(1, budget.SOFT, budget.DAILY) is True
    assert budget.claim_alert(2, budget.SOFT, budget.DAILY) is True


async def test_send_alert_noop_without_webhook():
    assert await budget.send_alert(1, budget.check(_principal())) is False


def test_alert_payload_describes_the_breach():
    budget.record_spend(1, 0.9)
    payload = budget.alert_payload(1, budget.check(_principal()))
    assert payload["kind"] == "soft"
    assert payload["period"] == "daily"
    assert payload["budget_usd"] == 1.0
    assert "soft cap" in payload["text"]


# --- enforcement through the pipeline ---------------------------------------


def test_request_under_cap_succeeds(client, stub_provider):
    assert client.post("/v1/chat/completions", json=PAYLOAD).status_code == 200


def test_hard_cap_returns_429_with_error_json(client, stub_provider):
    budget.record_spend(1, 999.0)
    resp = client.post("/v1/chat/completions", json=PAYLOAD)

    assert resp.status_code == 429
    error = resp.json()["detail"]["error"]
    assert error["type"] == "budget_exceeded"
    assert error["code"].endswith("hard_cap")


def test_soft_cap_forces_cheapest_tier(client, stub_provider):
    # 80% of the dev key's $10 daily budget.
    budget.record_spend(1, 8.0)
    hard_prompt = {
        "model": "claude-opus-5",
        "messages": [
            {"role": "system", "content": "You are a principal engineer. " * 40},
            {
                "role": "user",
                "content": "Refactor step by step.\n```python\n" + "x = 1\n" * 300 + "```",
            },
        ],
    }
    resp = client.post("/v1/chat/completions", json=hard_prompt)

    assert resp.status_code == 200
    assert resp.headers["x-autopilot-tier"] == "cheap"


def test_budget_event_recorded_once_per_window(client, session, stub_provider):
    budget.record_spend(1, 8.0)

    for _ in range(3):
        client.post("/v1/chat/completions", json=PAYLOAD)

    events = session.exec(select(BudgetEvent)).all()
    assert len(events) == 1
    assert events[0].kind == "soft"
    assert events[0].period == "daily"


def test_spend_counter_advances_with_requests(client, stub_provider):
    client.post("/v1/chat/completions", json=PAYLOAD)
    daily, monthly = budget.get_spend(1)
    assert daily > 0
    assert daily == pytest.approx(monthly)
