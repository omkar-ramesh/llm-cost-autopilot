"""Replay a mixed workload against the gateway twice - once pinned to the premium model
(the baseline) and once on autopilot - and report cost, savings, quality and latency.

Usage:
    make up && make bench                 # mock providers, no API keys needed
    BENCH_N=500 python scripts/benchmark.py --real   # real providers, needs keys
"""

import argparse
import os
import random
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.quality import JUDGE_SYSTEM, parse_judge_score  # noqa: E402

BASE_URL = os.environ.get("BENCH_URL", "http://localhost:8000")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "admin-dev-token")
BASELINE_MODEL = os.environ.get("BENCH_BASELINE_MODEL", "claude-opus-5")
JUDGE_MODEL = os.environ.get("BENCH_JUDGE_MODEL", "gpt-4o")

EASY = [
    "What is the capital of {country}?",
    "Translate '{word}' into French.",
    "Give me a one-sentence summary of what {thing} is.",
    "What day of the week was {date}?",
    "Rewrite this to be friendlier: 'Your request was denied.' (ticket {n})",
]

MEDIUM = [
    "Compare {a} and {b} for a write-heavy workload, and explain the trade-offs.",
    "Here is our event payload: {schema} Evaluate the trade-offs of flattening this schema "
    "and explain why.",
    "Analyze the following log line and tell me what went wrong: {log}",
    "Draft release notes for version {n} covering: auth fixes, faster search, new export.",
]

def _hard_refactor(code: str, thing: str, tag: str) -> list[dict]:
    return [
        {"role": "system", "content": "You are a principal engineer. " * 40},
        {"role": "user", "content": f"Refactor this module step by step.\n```python\n{code}```"},
        {"role": "assistant", "content": "Sure, which part first?"},
        {
            "role": "user",
            "content": "First analyze the hot path, then design a fix and explain why.",
        },
        {"role": "assistant", "content": "Understood."},
        {
            "role": "user",
            "content": f"Also prove the complexity bound: $$O(n \\log n)$$ (request {tag})",
        },
    ]


def _hard_failover(code: str, thing: str, tag: str) -> list[dict]:
    return [
        {"role": "system", "content": "You are a principal SRE. " * 40},
        {"role": "user", "content": f"Design a multi-region failover architecture for {thing}."},
        {"role": "assistant", "content": "Sure, where should we start?"},
        {
            "role": "user",
            "content": "First analyze the failure modes, then evaluate the trade-offs "
            "of each option.",
        },
        {"role": "assistant", "content": "Understood."},
        {
            "role": "user",
            "content": f"Then implement a rollout plan.\n```yaml\n{code}```(request {tag})",
        },
    ]


def _hard_deadlock(code: str, thing: str, tag: str) -> list[dict]:
    return [
        {"role": "system", "content": "You are a distributed-systems expert. " * 40},
        {
            "role": "user",
            "content": f"Debug this and explain why it deadlocks:\n```python\n{code}```",
        },
        {"role": "assistant", "content": "Let's work through it."},
        {
            "role": "user",
            "content": "Compare at least two approaches and justify the theorem behind "
            "your choice.",
        },
        {"role": "assistant", "content": "Understood."},
        {"role": "user", "content": f"Then optimize it and prove correctness. (request {tag})"},
    ]


HARD_BUILDERS = [_hard_refactor, _hard_failover, _hard_deadlock]

COUNTRIES = ["France", "Japan", "Brazil", "Kenya", "Norway", "Chile", "Egypt", "Nepal"]
WORDS = ["butterfly", "keyboard", "river", "silence", "harvest", "anchor"]
THINGS = ["a message queue", "a CDN", "a load balancer", "a vector database"]
PAIRS = [("Postgres", "MySQL"), ("Kafka", "RabbitMQ"), ("Redis", "Memcached")]


@dataclass
class Sample:
    difficulty: str
    prompt: str
    messages: list[dict] | None = None  # full conversation; falls back to a single user turn

    def as_messages(self) -> list[dict]:
        return self.messages or [{"role": "user", "content": self.prompt}]


@dataclass
class RunStats:
    label: str
    cost: float = 0.0
    baseline_cost: float = 0.0
    latencies: list[float] = field(default_factory=list)
    models: dict[str, int] = field(default_factory=dict)
    tiers: dict[str, int] = field(default_factory=dict)
    errors: int = 0
    answers: list[str] = field(default_factory=list)

    def record(self, resp: httpx.Response, elapsed_ms: float) -> None:
        if resp.status_code != 200:
            self.errors += 1
            self.answers.append("")
            return
        self.cost += float(resp.headers.get("x-autopilot-cost-usd", 0))
        self.baseline_cost += float(resp.headers.get("x-autopilot-cost-usd", 0)) + float(
            resp.headers.get("x-autopilot-savings-usd", 0)
        )
        self.latencies.append(elapsed_ms)
        model = resp.headers.get("x-autopilot-model", "?")
        tier = resp.headers.get("x-autopilot-tier", "?")
        self.models[model] = self.models.get(model, 0) + 1
        self.tiers[tier] = self.tiers.get(tier, 0) + 1
        self.answers.append(
            (resp.json().get("choices") or [{}])[0].get("message", {}).get("content", "")
        )

    def percentile(self, p: float) -> float:
        if not self.latencies:
            return 0.0
        ordered = sorted(self.latencies)
        idx = min(int(round(p * (len(ordered) - 1))), len(ordered) - 1)
        return ordered[idx]


def build_workload(n: int, seed: int = 7, salt: str = "") -> list[Sample]:
    """Deterministic mixed workload: 50% easy, 30% medium, 20% hard.

    `salt` makes prompts unique per run so a previous run's cached responses cannot
    be mistaken for routing savings.
    """
    rng = random.Random(seed)
    samples: list[Sample] = []

    for i in range(n):
        roll = i / n
        a, b = rng.choice(PAIRS)
        thing = rng.choice(THINGS)
        # Every prompt is unique, so the measured savings come from routing, not cache hits.
        tag = f"{salt}-{i}" if salt else str(i)

        if roll < 0.5:
            difficulty, template = "easy", rng.choice(EASY)
            prompt = template.format(
                country=rng.choice(COUNTRIES),
                word=rng.choice(WORDS),
                thing=thing,
                date=f"2026-0{rng.randint(1, 9)}-1{rng.randint(0, 9)}",
                n=i,
            )
            samples.append(Sample(difficulty, f"{prompt} (request {tag})"))
        elif roll < 0.8:
            difficulty, template = "medium", rng.choice(MEDIUM)
            prompt = template.format(
                a=a,
                b=b,
                n=i,
                schema='{"type": "object", "properties": {"id": {"type": "string"}}}' * 6,
                log=f"ERROR conn pool exhausted after {rng.randint(10, 900)}ms (req {i})",
            )
            samples.append(Sample(difficulty, f"{prompt} (request {tag})"))
        else:
            difficulty = "hard"
            # Larger code block than easy/medium prompts and a real multi-turn shape
            # (system prompt + prior turns) - both push the score past the premium threshold,
            # matching the router's own Phase 3 calibration fixture.
            code = f"def handler_{i}(x):\n    return x + 1\n" * rng.randint(150, 300)
            builder = rng.choice(HARD_BUILDERS)
            messages = builder(code, thing, tag)
            prompt = "\n---\n".join(m["content"] for m in messages)
            samples.append(Sample(difficulty, prompt, messages=messages))

    rng.shuffle(samples)
    return samples


def provision_key(client: httpx.Client) -> str:
    admin = {"x-admin-token": ADMIN_TOKEN}
    tenant = client.post(
        f"{BASE_URL}/v1/admin/tenants", headers=admin, json={"name": f"bench-{int(time.time())}"}
    )
    tenant.raise_for_status()
    key = client.post(
        f"{BASE_URL}/v1/admin/keys",
        headers=admin,
        json={
            "tenant_id": tenant.json()["id"],
            # Budgets must not throttle the benchmark itself.
            "daily_budget_usd": 1_000_000.0,
            "monthly_budget_usd": 1_000_000.0,
        },
    )
    key.raise_for_status()
    return key.json()["api_key"]


def run(client: httpx.Client, samples: list[Sample], api_key: str, mode: str, label: str):
    stats = RunStats(label)
    headers = {"Authorization": f"Bearer {api_key}", "x-autopilot-mode": mode}

    for i, sample in enumerate(samples, 1):
        body = {
            "model": BASELINE_MODEL,
            "messages": sample.as_messages(),
        }
        started = time.perf_counter()
        try:
            resp = client.post(f"{BASE_URL}/v1/chat/completions", headers=headers, json=body)
        except httpx.HTTPError:
            stats.errors += 1
            stats.answers.append("")
            continue
        stats.record(resp, (time.perf_counter() - started) * 1000)

        if i % 50 == 0:
            print(f"  {label}: {i}/{len(samples)}", flush=True)

    return stats


def judge(
    client: httpx.Client,
    api_key: str,
    samples: list[Sample],
    baseline: RunStats,
    autopilot: RunStats,
    limit: int,
) -> tuple[float | None, int]:
    """Score autopilot answers against the premium baseline answers."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "x-autopilot-model": JUDGE_MODEL,
    }

    candidates = [
        i
        for i in range(len(samples))
        if i < len(baseline.answers)
        and i < len(autopilot.answers)
        and baseline.answers[i]
        and autopilot.answers[i]
    ]
    if not candidates:
        return None, 0

    random.Random(11).shuffle(candidates)
    chosen = candidates[:limit]

    passed = 0
    judged = 0
    judge_errors = 0
    for n, i in enumerate(chosen, 1):
        body = {
            "model": JUDGE_MODEL,
            "messages": [
                {"role": "system", "content": JUDGE_SYSTEM},
                {
                    "role": "user",
                    "content": (
                        f"PROMPT:\n{samples[i].prompt}\n\n"
                        f"REFERENCE ANSWER:\n{baseline.answers[i]}\n\n"
                        f"CANDIDATE ANSWER:\n{autopilot.answers[i]}"
                    ),
                },
            ],
        }
        try:
            resp = client.post(f"{BASE_URL}/v1/chat/completions", headers=headers, json=body)
        except httpx.HTTPError as exc:
            judge_errors += 1
            print(f"  judge call failed (network): {exc}", file=sys.stderr)
            continue

        if resp.status_code != 200:
            # A gateway error (e.g. missing provider key, unknown judge model) must not be
            # silently scored as a fail - that hides a broken benchmark as "0% quality".
            judge_errors += 1
            print(f"  judge call failed ({resp.status_code}): {resp.text[:200]}", file=sys.stderr)
            continue

        content = (resp.json().get("choices") or [{}])[0].get("message", {}).get("content", "")
        score, _ = parse_judge_score(content)
        passed += int(score >= 0.8)
        judged += 1
        if n % 25 == 0:
            print(f"  judge: {n}/{len(chosen)}", flush=True)

    if judge_errors:
        print(f"  WARNING: {judge_errors}/{len(chosen)} judge calls failed - check "
              f"BENCH_JUDGE_MODEL is a model the gateway can actually reach.", file=sys.stderr)

    if judged == 0:
        return None, 0
    return passed / judged, judged


def format_report(
    samples: list[Sample],
    baseline: RunStats,
    autopilot: RunStats,
    pass_rate: float | None,
    judged: int,
    mock: bool,
) -> str:
    savings = baseline.cost - autopilot.cost
    pct = (savings / baseline.cost * 100) if baseline.cost else 0.0

    mix = {d: sum(1 for s in samples if s.difficulty == d) for d in ("easy", "medium", "hard")}
    routing = ", ".join(f"{t}: {c}" for t, c in sorted(autopilot.tiers.items()))
    quality = f"{pass_rate * 100:.1f}% ({judged} judged)" if pass_rate is not None else "n/a"

    lines = [
        "# Benchmark",
        "",
        f"**{len(samples)} prompts** ({mix['easy']} easy / {mix['medium']} medium / "
        f"{mix['hard']} hard), each replayed twice: once pinned to `{BASELINE_MODEL}` "
        "(baseline) and once through the autopilot router.",
        "",
        f"| Metric | Baseline (always {BASELINE_MODEL}) | Autopilot |",
        "| --- | --- | --- |",
        f"| Total cost | ${baseline.cost:.4f} | **${autopilot.cost:.4f}** |",
        f"| Savings | - | **${savings:.4f} ({pct:.1f}%)** |",
        f"| Judge pass rate | 100% (reference) | {quality} |",
        f"| p50 latency | {baseline.percentile(0.5):.0f} ms | "
        f"{autopilot.percentile(0.5):.0f} ms |",
        f"| p95 latency | {baseline.percentile(0.95):.0f} ms | "
        f"{autopilot.percentile(0.95):.0f} ms |",
        f"| Errors | {baseline.errors} | {autopilot.errors} |",
        "",
        f"Autopilot routing: {routing}",
        "",
        "Every prompt in the workload is unique, so the savings above come from routing "
        "alone - the response cache contributes nothing to these numbers.",
        "",
    ]

    if mock:
        lines += [
            "> **Mock mode.** The routing decisions and the price table are real, so the split "
            "of traffic across tiers is what the router would actually do on this workload. "
            "The dollar figures are not a production measurement: mock completions are a fixed "
            "~12 tokens regardless of model or difficulty, whereas real answers are longer and "
            "vary by prompt, and output tokens dominate premium-model cost. Latency and the "
            "judge pass rate are meaningless here - mock providers answer instantly and the "
            "mock judge always returns 0.9. Run `--real` with provider API keys for numbers "
            "worth quoting.",
            "",
        ]

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=int(os.environ.get("BENCH_N", 500)))
    parser.add_argument("--judge-sample", type=int, default=50)
    parser.add_argument(
        "--real", action="store_true", help="assume real providers (affects the report caveat only)"
    )
    parser.add_argument("--out", default=str(ROOT / "BENCHMARK.md"))
    args = parser.parse_args()

    with httpx.Client(timeout=120) as client:
        try:
            health = client.get(f"{BASE_URL}/healthz")
            health.raise_for_status()
        except httpx.HTTPError:
            print(f"Gateway not reachable at {BASE_URL}. Run `make up` first.", file=sys.stderr)
            return 1

        api_key = provision_key(client)
        samples = build_workload(args.n, salt=str(int(time.time())))
        print(f"Replaying {len(samples)} prompts against {BASE_URL}")

        baseline = run(client, samples, api_key, "quality", "baseline")
        autopilot = run(client, samples, api_key, "balanced", "autopilot")
        pass_rate, judged = judge(
            client, api_key, samples, baseline, autopilot, args.judge_sample
        )

    report = format_report(samples, baseline, autopilot, pass_rate, judged, mock=not args.real)
    Path(args.out).write_text(report, encoding="utf-8")
    print("\n" + report)
    print(f"Written to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build_workload", "format_report", "RunStats", "Sample"]
