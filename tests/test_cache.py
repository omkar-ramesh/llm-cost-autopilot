import pytest
from sqlmodel import select

from app import cache as cache_module
from app import providers
from app.models import Request
from app.pricing import cost_usd

PAYLOAD = {"model": "claude-opus-5", "messages": [{"role": "user", "content": "hi there"}]}


@pytest.fixture
def counting_provider(monkeypatch):
    calls = {"n": 0}

    async def fake_acompletion(**kwargs):
        calls["n"] += 1
        return {
            "id": "x",
            "model": kwargs["model"],
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50},
        }

    monkeypatch.setattr(providers, "acompletion", fake_acompletion)
    return calls


# --- key derivation ----------------------------------------------------------


def test_same_payload_same_key():
    assert cache_module.cache_key(PAYLOAD, "m") == cache_module.cache_key(dict(PAYLOAD), "m")


def test_key_ignores_field_order():
    a = {"model": "m", "messages": [], "temperature": 0}
    b = {"temperature": 0, "messages": [], "model": "m"}
    assert cache_module.cache_key(a, "m") == cache_module.cache_key(b, "m")


def test_key_ignores_non_semantic_fields():
    base = dict(PAYLOAD)
    tagged = {**PAYLOAD, "user": "alice", "metadata": {"trace": "123"}}
    assert cache_module.cache_key(base, "m") == cache_module.cache_key(tagged, "m")


@pytest.mark.parametrize(
    "change",
    [
        {"model": "gpt-4o"},
        {"messages": [{"role": "user", "content": "different"}]},
        {"max_tokens": 10},
        {"response_format": {"type": "json_object"}},
    ],
)
def test_semantic_fields_split_the_key(change):
    changed = cache_module.cache_key({**PAYLOAD, **change}, "m")
    assert changed != cache_module.cache_key(PAYLOAD, "m")


# --- cacheability ------------------------------------------------------------


def test_streaming_is_not_cacheable():
    assert not cache_module.is_cacheable({**PAYLOAD, "stream": True})


def test_nondeterministic_requests_are_not_cacheable():
    assert not cache_module.is_cacheable({**PAYLOAD, "temperature": 0.7})
    assert not cache_module.is_cacheable({**PAYLOAD, "n": 3})


def test_deterministic_request_is_cacheable():
    assert cache_module.is_cacheable(PAYLOAD)
    assert cache_module.is_cacheable({**PAYLOAD, "temperature": 0})


# --- backend -----------------------------------------------------------------


async def test_redis_cache_roundtrip(fake_redis):
    c = cache_module.RedisCache(ttl_s=60)
    assert await c.get("cache:missing") is None
    await c.set("cache:k", {"hello": "world"})
    assert await c.get("cache:k") == {"hello": "world"}


async def test_cache_outage_degrades_to_miss(monkeypatch):
    def boom():
        raise ConnectionError("redis down")

    monkeypatch.setattr(cache_module, "get_redis", boom)
    c = cache_module.RedisCache(ttl_s=60)

    assert await c.get("cache:k") is None
    await c.set("cache:k", {"a": 1})  # must not raise


async def test_null_cache_never_hits():
    c = cache_module.NullCache()
    await c.set("k", {"a": 1})
    assert await c.get("k") is None


# --- through the pipeline ----------------------------------------------------


def test_second_identical_request_is_served_from_cache(client, counting_provider, cache):
    first = client.post("/v1/chat/completions", json=PAYLOAD)
    second = client.post("/v1/chat/completions", json=PAYLOAD)

    assert first.json() == second.json()
    assert counting_provider["n"] == 1


def test_cache_hit_is_free_and_counts_full_savings(client, session, counting_provider, cache):
    client.post("/v1/chat/completions", json=PAYLOAD)
    hit = client.post("/v1/chat/completions", json=PAYLOAD)

    assert float(hit.headers["x-autopilot-cost-usd"]) == 0.0
    expected_baseline = cost_usd("claude-opus-5", 100, 50)
    assert float(hit.headers["x-autopilot-savings-usd"]) == pytest.approx(expected_baseline)

    rows = session.exec(select(Request).order_by(Request.id)).all()
    assert rows[1].cache_hit is True
    assert rows[1].cost_usd == 0.0
    assert rows[1].baseline_cost_usd == pytest.approx(expected_baseline)


def test_quality_mode_is_not_served_a_cheap_cached_answer(client, counting_provider, cache):
    """The routed model is part of the key, so modes cannot share an entry."""
    cheap = client.post("/v1/chat/completions", json=PAYLOAD)
    assert cheap.headers["x-autopilot-model"] == "gpt-4o-mini"

    premium = client.post(
        "/v1/chat/completions", json=PAYLOAD, headers={"x-autopilot-mode": "quality"}
    )
    assert premium.headers["x-autopilot-model"] == "claude-opus-5"
    assert counting_provider["n"] == 2


def test_different_prompts_do_not_share_cache(client, counting_provider, cache):
    client.post("/v1/chat/completions", json=PAYLOAD)
    client.post(
        "/v1/chat/completions",
        json={**PAYLOAD, "messages": [{"role": "user", "content": "something else"}]},
    )
    assert counting_provider["n"] == 2


def test_streaming_bypasses_cache(client, counting_provider, cache):
    async def fake_stream(**kwargs):
        async def gen():
            yield {"id": "1", "choices": [{"index": 0, "delta": {"content": "ok"}}]}

        return gen()

    client.post("/v1/chat/completions", json=PAYLOAD)

    import app.providers as p

    original = p.acompletion
    p.acompletion = fake_stream
    try:
        resp = client.post("/v1/chat/completions", json={**PAYLOAD, "stream": True})
        assert resp.status_code == 200
    finally:
        p.acompletion = original


def test_nondeterministic_request_always_calls_provider(client, counting_provider, cache):
    hot = {**PAYLOAD, "temperature": 0.9}
    client.post("/v1/chat/completions", json=hot)
    client.post("/v1/chat/completions", json=hot)
    assert counting_provider["n"] == 2

