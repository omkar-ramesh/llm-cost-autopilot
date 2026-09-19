# LLM Cost Autopilot

OpenAI-compatible gateway that cuts LLM spend automatically: tracks cost per request, routes easy
prompts to cheaper models, enforces budgets, and proves quality didn't drop.

## Quickstart

```bash
make up          # postgres + redis + api on :8000
curl localhost:8000/healthz
```

```bash
curl localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer sk-autopilot-dev" \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"hello"}]}'
```

Every response carries `x-autopilot-model`, `x-autopilot-cost-usd`, and `x-autopilot-savings-usd`,
and every request is logged to the `requests` table.

## Development

```bash
pip install -e ".[dev]"
make test
make lint
```
