# Decisions

- Added `app/db.py` (engine/session/seed) — layout listed no DB module; keeps `models.py` tables-only.
- Added `app/providers.py` — thin LiteLLM wrapper so tests patch one seam instead of the library.
- Dev API key seeded at startup: `sk-autopilot-dev` (admin key endpoints land in Phase 5).
- Tests run on SQLite via `DATABASE_URL` override; Postgres is used in Docker Compose.
- Phase 1 logs `tier="passthrough"` and `complexity_score=0.0` until the router lands in Phase 3.
