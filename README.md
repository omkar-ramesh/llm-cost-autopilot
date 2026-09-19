# LLM Cost Autopilot

OpenAI-compatible gateway that cuts LLM spend automatically: tracks cost per request, routes easy
prompts to cheaper models, enforces budgets, and proves quality didn't drop.

## Quickstart

```bash
make up          # api :8000, postgres, redis, prometheus :9090, grafana :3000
curl localhost:8000/healthz
```

Set `MOCK_PROVIDERS=true` to run the whole stack without any provider API keys.
Grafana (anonymous viewer, admin/admin) auto-provisions the **LLM Cost Autopilot** dashboard:
spend/hour, spend by model, tokens, p50/p95 latency, and savings.

```bash
curl localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer sk-autopilot-dev" \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"hello"}]}'
```

Every response carries `x-autopilot-model`, `x-autopilot-cost-usd`, `x-autopilot-savings-usd`,
`x-autopilot-tier`, and `x-autopilot-complexity`; every request is logged to the `requests` table.

### Routing

Each prompt gets a heuristic complexity score (0-1) that selects a tier — `cheap` (≤0.35),
`mid` (≤0.70), `premium` — and the request falls back up the chain on timeout/429/5xx.
Callers can steer per request:

| Header | Effect |
| --- | --- |
| `x-autopilot-mode: cheap\|balanced\|quality` | Force cheapest tier, score-based (default), or premium |
| `x-autopilot-model: <model>` | Pin an exact model and skip routing |

### Quality guard

A sampled share of routed-down requests (5% by default) is re-run on the originally
requested model and scored by a judge model. Scores land in the `evals` table; after
3 consecutive failures a prompt class is routed one tier higher for 24h.

```bash
curl localhost:8000/v1/autopilot/quality -H "Authorization: Bearer sk-autopilot-dev"
```

Shadow-eval spend is tracked separately (`autopilot_shadow_eval_cost_usd_total`) so the
cost of proving quality is never mistaken for savings.

### Budgets

Each API key carries a daily and monthly budget, counted in Redis. At 80% the gateway
alerts once per window (set `ALERT_WEBHOOK_URL` for Slack or any webhook) and forces the
cheapest tier; at 100% it returns `429` with a `budget_exceeded` error body.

Admin endpoints are guarded by `x-admin-token` (`ADMIN_TOKEN`, default `admin-dev-token`):

```bash
curl -X POST localhost:8000/v1/admin/tenants \
  -H "x-admin-token: admin-dev-token" -H "Content-Type: application/json" \
  -d '{"name":"acme"}'

curl -X POST localhost:8000/v1/admin/keys \
  -H "x-admin-token: admin-dev-token" -H "Content-Type: application/json" \
  -d '{"tenant_id":1,"daily_budget_usd":25,"monthly_budget_usd":500}'
```

`GET /v1/admin/tenants/{id}/spend` reports live spend; `PATCH /v1/admin/keys/{id}/budget`
updates budgets or deactivates a key.

## Development

```bash
pip install -e ".[dev]"
make test
make lint
```
