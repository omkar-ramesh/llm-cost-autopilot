# Decisions

- Added `app/db.py` (engine/session/seed) — layout listed no DB module; keeps `models.py` tables-only.
- Added `app/providers.py` — thin LiteLLM wrapper so tests patch one seam instead of the library.
- Dev API key seeded at startup: `sk-autopilot-dev` (admin key endpoints land in Phase 5).
- Tests run on SQLite via `DATABASE_URL` override; Postgres is used in Docker Compose.
- `MOCK_PROVIDERS=true` makes `providers.acompletion` return canned responses — lets `make up` and the benchmark run with no API keys.
- Metrics emit from `gateway._log_request` only, so DB rows and Prometheus counters cannot diverge.
- Savings counter only increments when savings > 0 (populated from Phase 3 onward, once the router routes down).
- Scorer weights calibrated against a sample spread, not fitted to one prompt: code/math/schema signals (0.35) outweigh conversation turns (0.12), since turn count is a weak difficulty signal.
- Fallback chain = chosen tier then every tier above it, so a retryable failure escalates quality rather than degrading it.
- `fallback_on` entries are normalised to strings — YAML parses bare `429` as an int, which silently disabled 429 retries.
- Phase 1 logs `tier="passthrough"` and `complexity_score=0.0` until the router lands in Phase 3.
