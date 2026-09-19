# Benchmark

**500 prompts** (250 easy / 150 medium / 100 hard), each replayed twice: once pinned to `claude-opus-5` (baseline) and once through the autopilot router.

| Metric | Baseline (always claude-opus-5) | Autopilot |
| --- | --- | --- |
| Total cost | $1.2649 | **$0.1368** |
| Savings | - | **$1.1281 (89.2%)** |
| Judge pass rate | 100% (reference) | 100.0% (50 judged) |
| p50 latency | 48 ms | 48 ms |
| p95 latency | 49 ms | 51 ms |
| Errors | 0 | 0 |

Autopilot routing: cheap: 366, mid: 134

Every prompt in the workload is unique, so the savings above come from routing alone - the response cache contributes nothing to these numbers.

> **Mock mode.** The routing decisions and the price table are real, so the split of traffic across tiers is what the router would actually do on this workload. The dollar figures are not a production measurement: mock completions are a fixed ~12 tokens regardless of model or difficulty, whereas real answers are longer and vary by prompt, and output tokens dominate premium-model cost. Latency and the judge pass rate are meaningless here - mock providers answer instantly and the mock judge always returns 0.9. Run `--real` with provider API keys for numbers worth quoting.
