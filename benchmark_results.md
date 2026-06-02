# Scheduler benchmark

All strategies, all scenarios. Timings measured at WARNING log level so they reflect the algorithm, not console I/O. Lower cost/wait is better.

| Scenario | Strategy | Cost | Total wait | Max wait | Seconds |
| --- | --- | ---: | ---: | ---: | ---: |
| Scenario 1 - Even spacing | global_greedy | 55.0 | 30 | 10 | 0.114 |
| Scenario 1 - Even spacing | local_search | 55.0 | 30 | 10 | 0.264 |
| Scenario 1 - Even spacing | selfish_greedy | 1050.0 | 900 | 90 | 0.006 |
| Scenario 2 - Bunched start | global_greedy | 372.0 | 310 | 32 | 0.101 |
| Scenario 2 - Bunched start | local_search | 372.0 | 310 | 32 | 0.248 |
| Scenario 2 - Bunched start | selfish_greedy | 1666.0 | 1446 | 132 | 0.007 |
| Scenario 3 - Asymmetric load | global_greedy | 35.0 | 15 | 10 | 0.051 |
| Scenario 3 - Asymmetric load | local_search | 0.0 | 0 | 0 | 0.412 |
| Scenario 3 - Asymmetric load | selfish_greedy | 600.0 | 450 | 90 | 0.007 |
| Scenario 4 - Operator-heavy | global_greedy | 60.0 | 30 | 10 | 0.181 |
| Scenario 4 - Operator-heavy | local_search | 60.0 | 30 | 10 | 0.630 |
| Scenario 4 - Operator-heavy | selfish_greedy | 1430.0 | 900 | 90 | 0.013 |
| Scenario 5 - Worst case convergence | global_greedy | 519.0 | 440 | 44 | 0.203 |
| Scenario 5 - Worst case convergence | local_search | 455.0 | 400 | 52 | 0.718 |
| Scenario 5 - Worst case convergence | selfish_greedy | 1785.0 | 1530 | 153 | 0.006 |

## Strategy quality (best cost per scenario)

| Scenario | Best strategy | Best cost |
| --- | --- | ---: |
| Scenario 1 - Even spacing | global_greedy | 55.0 |
| Scenario 2 - Bunched start | global_greedy | 372.0 |
| Scenario 3 - Asymmetric load | local_search | 0.0 |
| Scenario 4 - Operator-heavy | global_greedy | 60.0 |
| Scenario 5 - Worst case convergence | local_search | 455.0 |

Note: `local_search` now seeds from `global_greedy`, so it starts from the stronger greedy and only ever improves on it -- it is never worse than `global_greedy` and is strictly better where a single-trip swap escapes the greedy's local optimum (e.g. it reaches a zero-wait schedule on the asymmetric scenario). `selfish_greedy` alone is poor; it exists as a cheap, naive baseline to show the gap good scoring closes. This is why the factory default is `local_search`.

## Why local search felt slow

Same local-search run on a 20-trip scenario, timed twice:

- with **DEBUG logging on**: 2.543 s
- with **logging quiet** (WARNING): 0.175 s
- logging overhead: **2.368 s** (~14.5x slower with DEBUG on)

The algorithm is identical to `main.py`'s STEP 8 hill-climb; the wall-clock difference is dominated by (a) emitting thousands of DEBUG lines while re-simulating every single-trip-swap neighbour, and (b) building frozen Pydantic value objects (`ChargeEvent`, `ChargingPlan`) on every re-simulation. `main.py`'s demo also ran on a much smaller fleet.
