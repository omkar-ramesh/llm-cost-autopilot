# Benchmark

**100 prompts** (50 easy / 30 medium / 20 hard), each replayed twice: once pinned to `claude-opus-5` (baseline) and once through the autopilot router.

| Metric | Baseline (always claude-opus-5) | Autopilot |
| --- | --- | --- |
| Total cost | $3.9091 | **$0.0640** |
| Savings | - | **$3.8452 (98.4%)** |
| Judge pass rate | 100% (reference) | 80.0% (50 judged) |
| p50 latency | 6146 ms | 282 ms |
| p95 latency | 18090 ms | 4959 ms |
| Errors | 0 | 0 |

Autopilot routing: cheap: 72, mid: 8, premium: 20

Every prompt in the workload is unique, so the savings above come from routing alone - the response cache contributes nothing to these numbers.
