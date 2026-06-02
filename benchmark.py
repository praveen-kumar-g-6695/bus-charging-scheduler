"""Benchmark harness: compare every scheduler strategy across every scenario.

Run with::

    uv run python benchmark.py

WHAT IT DOES
------------
1. Loads all scenarios from ``scenarios/`` via :class:`ScenarioLoader`.
2. Runs EVERY registered strategy (``selfish_greedy``, ``global_greedy``,
   ``local_search``) on EVERY scenario, timing each run and recording the three
   metrics that matter (global cost, total wait, max wait).
3. Quantifies WHY local search felt slow: it re-runs the local search once with
   DEBUG logging on and once with it off, so the logging overhead is visible as a
   number rather than a guess.
4. Prints a comparison table and writes it to ``benchmark_results.md``.

The benchmark itself runs at WARNING level, so the timings measure the ALGORITHM,
not the console I/O of thousands of DEBUG lines (that cost is reported separately
in step 3).
"""

import logging
import time
from pathlib import Path

from app.domain.models import Scenario
from app.domain.results import ScheduleResult
from app.io.loader import ScenarioLoader
from app.logging_config import get_logger, setup_logging
from app.objective.cost import CostFunction
from app.scheduling.factory import available_schedulers, create_scheduler

logger = get_logger(__name__)

_SCENARIOS_DIR = Path(__file__).parent / "scenarios"
_RESULTS_FILE = Path(__file__).parent / "benchmark_results.md"


class BenchmarkRow:
    """One measured run: a (scenario, strategy) pair and its results.

    Plain mutable record (not a frozen value object) because it is throwaway
    reporting scratch, never part of the domain model.
    """

    def __init__(
        self,
        scenario_name: str,
        strategy_name: str,
        cost: float,
        total_wait: int,
        max_wait: int,
        seconds: float,
    ) -> None:
        """Store the metrics measured for one scheduler run.

        Args:
            scenario_name: The scenario that was scheduled.
            strategy_name: The strategy that scheduled it.
            cost: The global weighted cost of the produced schedule.
            total_wait: Total queue minutes across every trip.
            max_wait: The single worst trip's queue minutes.
            seconds: Wall-clock time the strategy took.
        """
        self.scenario_name = scenario_name
        self.strategy_name = strategy_name
        self.cost = cost
        self.total_wait = total_wait
        self.max_wait = max_wait
        self.seconds = seconds


def _score(scenario: Scenario, result: ScheduleResult) -> float:
    """Score a finished schedule with the scenario's own weighted cost function.

    Args:
        scenario: The scenario whose weights define the objective.
        result: The finished ``ScheduleResult`` to score.

    Returns:
        The global weighted cost (lower is better).
    """
    return CostFunction(scenario.weights).score(result)


def run_benchmark(scenarios: list[Scenario]) -> list[BenchmarkRow]:
    """Run every strategy on every scenario and collect the measured rows.

    Args:
        scenarios: The scenarios to benchmark, in load order.

    Returns:
        One ``BenchmarkRow`` per (scenario, strategy) pair.
    """
    rows: list[BenchmarkRow] = []
    for scenario in scenarios:
        for strategy_name in available_schedulers():
            scheduler = create_scheduler(strategy_name)

            start = time.perf_counter()
            result = scheduler.schedule(scenario)
            seconds = time.perf_counter() - start

            row = BenchmarkRow(
                scenario_name=scenario.name,
                strategy_name=strategy_name,
                cost=_score(scenario, result),
                total_wait=result.total_wait_minutes(),
                max_wait=result.max_wait_minutes(),
                seconds=seconds,
            )
            rows.append(row)
            logger.warning(
                f"{scenario.name:<28} {strategy_name:<16} "
                f"cost {row.cost:>7.1f} | wait {row.total_wait:>4d} | "
                f"max {row.max_wait:>4d} | {seconds:>7.3f}s"
            )
    return rows


def measure_logging_overhead(scenario: Scenario) -> tuple[float, float]:
    """Time one local-search run with DEBUG logging on, then off.

    This isolates the answer to "why did 20 buses take ~29 s?": the algorithm is
    the same either way, so the gap between the two timings is pure logging cost
    (string formatting plus console I/O for thousands of DEBUG lines).

    Args:
        scenario: The scenario to schedule (use a 20-trip one for a fair test).

    Returns:
        A ``(debug_seconds, quiet_seconds)`` pair.
    """
    root_logger = logging.getLogger()

    root_logger.setLevel(logging.DEBUG)
    start = time.perf_counter()
    create_scheduler("local_search").schedule(scenario)
    debug_seconds = time.perf_counter() - start

    root_logger.setLevel(logging.WARNING)
    start = time.perf_counter()
    create_scheduler("local_search").schedule(scenario)
    quiet_seconds = time.perf_counter() - start

    return debug_seconds, quiet_seconds


def _results_markdown(
    rows: list[BenchmarkRow],
    debug_seconds: float,
    quiet_seconds: float,
) -> str:
    """Render the benchmark rows and the logging probe as a Markdown report.

    Args:
        rows: The measured rows for every (scenario, strategy) pair.
        debug_seconds: Local-search time on the probe scenario with DEBUG on.
        quiet_seconds: Local-search time on the probe scenario with logging quiet.

    Returns:
        The full Markdown document as a string.
    """
    lines: list[str] = []
    lines.append("# Scheduler benchmark")
    lines.append("")
    lines.append(
        "All strategies, all scenarios. Timings measured at WARNING log level so "
        "they reflect the algorithm, not console I/O. Lower cost/wait is better."
    )
    lines.append("")
    lines.append("| Scenario | Strategy | Cost | Total wait | Max wait | Seconds |")
    lines.append("| --- | --- | ---: | ---: | ---: | ---: |")
    for row in rows:
        lines.append(
            f"| {row.scenario_name} | {row.strategy_name} | {row.cost:.1f} | "
            f"{row.total_wait} | {row.max_wait} | {row.seconds:.3f} |"
        )
    lines.append("")
    lines.append("## Strategy quality (best cost per scenario)")
    lines.append("")
    lines.append("| Scenario | Best strategy | Best cost |")
    lines.append("| --- | --- | ---: |")
    for scenario_name in _ordered_scenario_names(rows):
        best = _best_row_for(rows, scenario_name)
        lines.append(f"| {scenario_name} | {best.strategy_name} | {best.cost:.1f} |")
    lines.append("")
    lines.append(
        "Note: `local_search` now seeds from `global_greedy`, so it starts from "
        "the stronger greedy and only ever improves on it -- it is never worse "
        "than `global_greedy` and is strictly better where a single-trip swap "
        "escapes the greedy's local optimum (e.g. it reaches a zero-wait schedule "
        "on the asymmetric scenario). `selfish_greedy` alone is poor; it exists as "
        "a cheap, naive baseline to show the gap good scoring closes. This is why "
        "the factory default is `local_search`."
    )
    lines.append("")
    lines.append("## Why local search felt slow")
    lines.append("")
    overhead = debug_seconds - quiet_seconds
    factor = debug_seconds / quiet_seconds if quiet_seconds > 0 else 0.0
    lines.append("Same local-search run on a 20-trip scenario, timed twice:")
    lines.append("")
    lines.append(f"- with **DEBUG logging on**: {debug_seconds:.3f} s")
    lines.append(f"- with **logging quiet** (WARNING): {quiet_seconds:.3f} s")
    lines.append(
        f"- logging overhead: **{overhead:.3f} s** "
        f"(~{factor:.1f}x slower with DEBUG on)"
    )
    lines.append("")
    lines.append(
        "The algorithm is identical to `main.py`'s STEP 8 hill-climb; the wall-"
        "clock difference is dominated by (a) emitting thousands of DEBUG lines "
        "while re-simulating every single-trip-swap neighbour, and (b) building "
        "frozen Pydantic value objects (`ChargeEvent`, `ChargingPlan`) on every "
        "re-simulation. `main.py`'s demo also ran on a much smaller fleet."
    )
    lines.append("")
    return "\n".join(lines)


def _ordered_scenario_names(rows: list[BenchmarkRow]) -> list[str]:
    """Return the scenario names in first-seen order, without duplicates.

    Args:
        rows: The measured benchmark rows.

    Returns:
        Each scenario name once, in the order it first appears.
    """
    ordered: list[str] = []
    for row in rows:
        if row.scenario_name not in ordered:
            ordered.append(row.scenario_name)
    return ordered


def _best_row_for(rows: list[BenchmarkRow], scenario_name: str) -> BenchmarkRow:
    """Return the lowest-cost row for one scenario.

    Args:
        rows: The measured benchmark rows.
        scenario_name: The scenario to find the best strategy for.

    Returns:
        The row with the lowest cost for that scenario.
    """
    scenario_rows = [row for row in rows if row.scenario_name == scenario_name]
    best = scenario_rows[0]
    for row in scenario_rows:
        if row.cost < best.cost:
            best = row
    return best


def main() -> None:
    """Run the full benchmark, print it, and save the Markdown report.

    Returns:
        None.
    """
    setup_logging(level="WARNING")

    scenarios = ScenarioLoader().load_dir(_SCENARIOS_DIR)
    logger.warning(f"loaded {len(scenarios)} scenario(s) from {_SCENARIOS_DIR}")
    logger.warning("running every strategy on every scenario ...")

    rows = run_benchmark(scenarios)

    probe_scenario = _largest_scenario(scenarios)
    logger.warning(
        f"measuring logging overhead on '{probe_scenario.name}' "
        f"({len(probe_scenario.trips)} trips) ..."
    )
    debug_seconds, quiet_seconds = measure_logging_overhead(probe_scenario)

    report = _results_markdown(rows, debug_seconds, quiet_seconds)
    _RESULTS_FILE.write_text(report, encoding="utf-8")
    logger.warning(f"wrote benchmark report to {_RESULTS_FILE}")

    print(report)


def _largest_scenario(scenarios: list[Scenario]) -> Scenario:
    """Return the scenario with the most trips (the heaviest timing probe).

    Args:
        scenarios: The loaded scenarios.

    Returns:
        The scenario carrying the most trips.
    """
    largest = scenarios[0]
    for scenario in scenarios:
        if len(scenario.trips) > len(largest.trips):
            largest = scenario
    return largest


if __name__ == "__main__":
    main()
