import pytest

from app import budget
from app.config import get_settings

ADMIN = {"x-admin-token": get_settings().admin_token}
PAYLOAD = {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]}


@pytest.fixture
def admin_client(client):
    client.headers.pop("Authorization", None)
    return client


def test_admin_requires_token(admin_client):
    assert admin_client.post("/v1/admin/tenants", json={"name": "acme"}).status_code == 401


def test_admin_rejects_wrong_token(admin_client):
    resp = admin_client.post(
        "/v1/admin/tenants", json={"name": "acme"}, headers={"x-admin-token": "nope"}
    )
    assert resp.status_code == 401


def test_create_tenant(admin_client):
    resp = admin_client.post("/v1/admin/tenants", json={"name": "acme"}, headers=ADMIN)
    assert resp.status_code == 201
    assert resp.json()["name"] == "acme"
    assert resp.json()["id"] > 0


def test_create_key_returns_raw_key_once(admin_client):
    tenant = admin_client.post(
        "/v1/admin/tenants", json={"name": "acme"}, headers=ADMIN
    ).json()

    resp = admin_client.post(
        "/v1/admin/keys",
        json={"tenant_id": tenant["id"], "daily_budget_usd": 5, "monthly_budget_usd": 50},
        headers=ADMIN,
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["api_key"].startswith("sk-ap-")
    assert body["daily_budget_usd"] == 5


def test_created_key_authenticates_against_the_gateway(admin_client, monkeypatch):
    from app import providers

    async def fake_acompletion(**kwargs):
        return {
            "id": "x",
            "model": kwargs["model"],
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }

    monkeypatch.setattr(providers, "acompletion", fake_acompletion)

    tenant = admin_client.post("/v1/admin/tenants", json={"name": "acme"}, headers=ADMIN).json()
    key = admin_client.post(
        "/v1/admin/keys", json={"tenant_id": tenant["id"]}, headers=ADMIN
    ).json()

    resp = admin_client.post(
        "/v1/chat/completions",
        json=PAYLOAD,
        headers={"Authorization": f"Bearer {key['api_key']}"},
    )
    assert resp.status_code == 200


def test_create_key_for_missing_tenant_is_404(admin_client):
    resp = admin_client.post("/v1/admin/keys", json={"tenant_id": 9999}, headers=ADMIN)
    assert resp.status_code == 404


def test_update_budget(admin_client):
    tenant = admin_client.post("/v1/admin/tenants", json={"name": "acme"}, headers=ADMIN).json()
    key = admin_client.post(
        "/v1/admin/keys", json={"tenant_id": tenant["id"]}, headers=ADMIN
    ).json()

    resp = admin_client.patch(
        f"/v1/admin/keys/{key['id']}/budget",
        json={"daily_budget_usd": 42.0},
        headers=ADMIN,
    )
    assert resp.status_code == 200
    assert resp.json()["daily_budget_usd"] == 42.0
    # Unspecified fields are left alone.
    assert resp.json()["monthly_budget_usd"] == 200.0


def test_deactivated_key_is_rejected(admin_client):
    tenant = admin_client.post("/v1/admin/tenants", json={"name": "acme"}, headers=ADMIN).json()
    key = admin_client.post(
        "/v1/admin/keys", json={"tenant_id": tenant["id"]}, headers=ADMIN
    ).json()

    admin_client.patch(
        f"/v1/admin/keys/{key['id']}/budget", json={"active": False}, headers=ADMIN
    )

    resp = admin_client.post(
        "/v1/chat/completions",
        json=PAYLOAD,
        headers={"Authorization": f"Bearer {key['api_key']}"},
    )
    assert resp.status_code == 401


def test_negative_budget_rejected(admin_client):
    tenant = admin_client.post("/v1/admin/tenants", json={"name": "acme"}, headers=ADMIN).json()
    resp = admin_client.post(
        "/v1/admin/keys",
        json={"tenant_id": tenant["id"], "daily_budget_usd": -1},
        headers=ADMIN,
    )
    assert resp.status_code == 422


def test_tenant_spend_report(admin_client):
    tenant = admin_client.post("/v1/admin/tenants", json={"name": "acme"}, headers=ADMIN).json()
    admin_client.post("/v1/admin/keys", json={"tenant_id": tenant["id"]}, headers=ADMIN)
    budget.record_spend(tenant["id"], 1.25)

    resp = admin_client.get(f"/v1/admin/tenants/{tenant['id']}/spend", headers=ADMIN)
    assert resp.status_code == 200
    assert resp.json()["daily_spend_usd"] == 1.25
    assert len(resp.json()["keys"]) == 1


def test_missing_tenant_spend_is_404(admin_client):
    assert admin_client.get("/v1/admin/tenants/9999/spend", headers=ADMIN).status_code == 404
