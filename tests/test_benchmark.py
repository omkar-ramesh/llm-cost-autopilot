import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.benchmark import RunStats, build_workload, format_report  # noqa: E402


def test_workload_is_deterministic():
    assert [s.prompt for s in build_workload(50)] == [s.prompt for s in build_workload(50)]


def test_workload_mix_is_50_30_20():
    samples = build_workload(500)
    counts = {d: sum(1 for s in samples if s.difficulty == d) for d in ("easy", "medium", "hard")}
    assert counts == {"easy": 250, "medium": 150, "hard": 100}


def test_salt_makes_runs_distinct():
    """Without this, a second benchmark run would be served the first run's cache."""
    a = {s.prompt for s in build_workload(20, salt="run-a")}
    b = {s.prompt for s in build_workload(20, salt="run-b")}
    assert a.isdisjoint(b)


def test_every_prompt_is_unique():
    samples = build_workload(500)
    assert len({s.prompt for s in samples}) == 500


def test_percentiles_from_latencies():
    stats = RunStats("x")
    stats.latencies = [float(i) for i in range(1, 101)]
    assert stats.percentile(0.5) == 51.0
    assert stats.percentile(0.95) == 95.0


def test_percentile_of_empty_run_is_zero():
    assert RunStats("x").percentile(0.95) == 0.0


def test_report_computes_savings_percentage():
    baseline = RunStats("baseline", cost=10.0)
    autopilot = RunStats("autopilot", cost=2.5)
    autopilot.tiers = {"cheap": 400, "premium": 100}

    report = format_report(build_workload(10), baseline, autopilot, 0.94, 50, mock=False)

    assert "$7.5000 (75.0%)" in report
    assert "94.0% (50 judged)" in report
    assert "Mock mode" not in report


def test_mock_report_carries_the_caveat():
    report = format_report(
        build_workload(10), RunStats("b", cost=1.0), RunStats("a"), None, 0, True
    )
    assert "Mock mode" in report
    assert "n/a" in report
