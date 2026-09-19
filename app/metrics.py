from prometheus_client import CollectorRegistry, Counter, Histogram

REGISTRY = CollectorRegistry()

LATENCY_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60)

requests_total = Counter(
    "autopilot_requests_total",
    "Chat completion requests handled",
    ["tenant_id", "routed_model", "tier", "status"],
    registry=REGISTRY,
)

spend_usd_total = Counter(
    "autopilot_spend_usd_total",
    "Actual spend in USD on the routed model",
    ["tenant_id", "routed_model"],
    registry=REGISTRY,
)

baseline_spend_usd_total = Counter(
    "autopilot_baseline_spend_usd_total",
    "What the same traffic would have cost on the requested model",
    ["tenant_id", "requested_model"],
    registry=REGISTRY,
)

savings_usd_total = Counter(
    "autopilot_savings_usd_total",
    "Baseline spend minus actual spend",
    ["tenant_id"],
    registry=REGISTRY,
)

tokens_total = Counter(
    "autopilot_tokens_total",
    "Tokens consumed",
    ["tenant_id", "routed_model", "kind"],
    registry=REGISTRY,
)

cache_hits_total = Counter(
    "autopilot_cache_hits_total",
    "Requests served from cache",
    ["tenant_id"],
    registry=REGISTRY,
)

fallbacks_total = Counter(
    "autopilot_fallbacks_total",
    "Times a model in the chain failed and the next was tried",
    ["failed_model"],
    registry=REGISTRY,
)

evals_total = Counter(
    "autopilot_evals_total",
    "Shadow evals completed",
    ["tier", "passed"],
    registry=REGISTRY,
)

escalations_total = Counter(
    "autopilot_escalations_total",
    "Prompt classes escalated a tier after repeated shadow-eval failures",
    ["prompt_class"],
    registry=REGISTRY,
)

shadow_eval_cost_usd_total = Counter(
    "autopilot_shadow_eval_cost_usd_total",
    "Spend on baseline + judge calls made for shadow evaluation",
    registry=REGISTRY,
)

shadow_eval_errors_total = Counter(
    "autopilot_shadow_eval_errors_total",
    "Shadow evals that failed to complete",
    registry=REGISTRY,
)

request_latency_seconds = Histogram(
    "autopilot_request_latency_seconds",
    "End-to-end gateway latency",
    ["routed_model", "tier"],
    buckets=LATENCY_BUCKETS,
    registry=REGISTRY,
)


def record_request(
    *,
    tenant_id: int,
    requested_model: str,
    routed_model: str,
    tier: str,
    status: str,
    prompt_tokens: int,
    completion_tokens: int,
    cost_usd: float,
    baseline_cost_usd: float,
    latency_ms: int,
    cache_hit: bool,
) -> None:
    tenant = str(tenant_id)
    requests_total.labels(tenant, routed_model, tier, status).inc()
    request_latency_seconds.labels(routed_model, tier).observe(latency_ms / 1000)

    if cache_hit:
        cache_hits_total.labels(tenant).inc()

    if cost_usd:
        spend_usd_total.labels(tenant, routed_model).inc(cost_usd)
    if baseline_cost_usd:
        baseline_spend_usd_total.labels(tenant, requested_model).inc(baseline_cost_usd)

    savings = baseline_cost_usd - cost_usd
    if savings > 0:
        savings_usd_total.labels(tenant).inc(savings)

    if prompt_tokens:
        tokens_total.labels(tenant, routed_model, "prompt").inc(prompt_tokens)
    if completion_tokens:
        tokens_total.labels(tenant, routed_model, "completion").inc(completion_tokens)
