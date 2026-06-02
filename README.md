# Bus Charging Scheduler

A scheduler for electric buses on the **Bengaluru ↔ Kochi** route. Each bus has a
limited battery range and must recharge at one of four stations (**A, B, C, D**)
along the way. Every station has a single charger, so when several buses want to
charge at the same place around the same time, the scheduler decides **which
stations each bus uses** and **what order buses take the charger** — guided by
tunable weights for three goals (individual wait, operator fairness, overall
efficiency).

The app is a single **Python + Streamlit** project: the scheduling engine, the
scenario data, and the web UI all live in one repository and run in one process.

---

## What the app does

Pick a scenario from a dropdown and the app shows you three things:

1. **Scenario input** — the route, stations, weights and the bus timetable being fed in.
2. **Per-bus timetable** — for each bus: which stations it charges at, how long it
   waits, and when it finally arrives.
3. **Per-station view** — for each of A, B, C, D: the order buses charged there.

That's the whole product: *pick a scenario → see the input → see what the scheduler decided.*

---

## Quick start (run it locally)

This project uses [**uv**](https://docs.astral.sh/uv/) to manage Python and dependencies.

```powershell
# 1. From the project root (the folder containing this README)
uv sync                 # creates the virtual env and installs dependencies

# 2. Run the web app
uv run streamlit run app/ui/streamlit_app.py
```

Streamlit opens a browser tab automatically. Pick a scenario from the dropdown at
the top and explore the three views.

> **Don't use uv?** Any environment with `pip install -r requirements.txt` works too:
> ```powershell
> pip install -r requirements.txt
> streamlit run app/ui/streamlit_app.py
> ```

### Run the scheduler without the UI

`main_prod.py` runs the engine from the command line and prints a walkthrough of
each step (plans, simulation, rules, cost, greedy, local search):

```powershell
uv run python main_prod.py
```

---

## Project layout

```
.
├── app/
│   ├── domain/        # the data model: Route, Station, Bus, Trip, Weights, Scenario, results
│   ├── rules/         # scoring objectives (individual / operator / overall) + the registry
│   ├── objective/     # CostFunction: the weighted sum of all rules
│   ├── scheduling/    # the schedulers (greedy variants + local search) + the factory
│   ├── io/            # ScenarioLoader: turns a YAML file into a validated Scenario
│   └── ui/            # the Streamlit app (presentation only)
├── scenarios/         # the 5 scenarios, each a self-describing YAML file
├── main_prod.py       # command-line walkthrough of the engine
├── requirements.txt   # runtime dependencies (what Streamlit Cloud installs)
└── ARCHITECTURE.md    # design decisions, data model, anticipated changes
```

A useful way to read the code: **input → engine → output.**
A `scenarios/*.yaml` file is the input; `ScenarioLoader` turns it into a
`Scenario`; a scheduler turns that into a `ScheduleResult`; the UI displays it.

---

## How to change a weight

The three weights — `individual`, `operator`, `overall` — live in **one obvious
place**: the `weights` block of each scenario file. Nothing in the engine is
hardcoded, so tuning is pure data.

Open the scenario you want to tune, e.g. `scenarios/scenario_4_operator_heavy.yaml`,
and edit the numbers:

```yaml
weights:
  individual: 1.0
  operator: 2.0     # <-- raise this to make operator fairness matter more
  overall: 1.0
```

Re-pick that scenario in the UI (or re-run `main_prod.py`) and the schedule
changes accordingly. Higher `operator` makes the scheduler work harder to balance
waiting time fairly across KPN / Freshbus / Flixbus, for example.

**Why it's this simple:** the cost function scores a schedule as
`sum(weight[rule] × rule.cost(schedule))`. Each weight is looked up by name via
`Weights.for_rule(...)`, and a weight that isn't set simply defaults to `0.0` (the
rule contributes nothing). So a weight is one value in one place — never scattered
through the code.

---

## How to add a new rule

A "rule" is one scoring objective (a number measuring how *bad* a finished
schedule is for that goal). Adding one is **two steps and touches no engine code**:

### Step 1 — Write the rule and register it

Add a class in `app/rules/builtin.py` (or your own module), implement `key` and
`cost`, and decorate it with `@register_rule`:

```python
from app.rules.base import Rule, RuleContext
from app.rules.registry import register_rule


@register_rule
class LateArrivalRule(Rule):
    """Penalise buses that arrive after a target time."""

    @property
    def key(self) -> str:
        return "late_arrival"          # the name its weight is looked up under

    def cost(self, context: RuleContext) -> float:
        result = context.result        # the finished schedule
        target_minute = 300
        late_minutes = 0
        for timeline in result.trip_timelines:
            overshoot = timeline.arrival_minute - target_minute
            if overshoot > 0:
                late_minutes += overshoot
        return float(late_minutes)     # 0 == perfect, higher == worse
```

That's it for the code. The `@register_rule` decorator adds the rule to the shared
registry; the cost function automatically picks up **every** registered rule, so
there is no central list to update and **no scheduler change**.

### Step 2 — Give it a weight in any scenario

The new rule only counts where you weight it. Add its key to a scenario's `weights`:

```yaml
weights:
  individual: 1.0
  operator: 1.0
  overall: 1.0
  late_arrival: 3.0     # <-- the new objective, weighted in via data only
```

Leave the key out (or set `0.0`) and the rule simply has no effect there.

**Why it's this small:** rules are discovered through a registry, the cost
function sums whatever is registered, and weights are a free-form name→number map.
Adding an objective never means editing the engine, the simulator, or the
schedulers — exactly the kind of growth the design is built for.

---

## The 5 scenarios

All five ship in `scenarios/` as self-describing YAML. Each file fully describes
its situation — route, stations, weights and the bus timetable — so the engine
reads only data and hardcodes nothing.

| File | Scenario | What it stresses |
|------|----------|------------------|
| `scenario_1_even_spacing.yaml` | Even spacing | Baseline: buses every 15 min |
| `scenario_2_bunched_start.yaml` | Bunched start | Heavy early contention |
| `scenario_3_asymmetric_load.yaml` | Asymmetric load | Uneven traffic between directions |
| `scenario_4_operator_heavy.yaml` | Operator-heavy | One operator dominates; `operator` weight = 2.0 |
| `scenario_5_worst_case.yaml` | Worst case | Maximum contention at inner stations |

---

## How it works in one paragraph

The scheduler treats each bus's **charging plan** (which stations it uses) and the
**global schedule** (everyone placed on shared chargers over time) as separate
concerns. A *simulator* drives each bus along the route, queues it at busy
chargers, and produces a timeline. A *cost function* turns that timeline into a
single weighted number from the active rules. A *greedy* scheduler builds a good
first schedule, and a *local search* refines it by trying single-bus plan swaps
that lower the global cost — always respecting the 240 km range limit. The full
reasoning, the data-model design, and the list of future changes the design
anticipates are in **[ARCHITECTURE.md](ARCHITECTURE.md)**.

---

## Tech notes

- **Python:** 3.10+
- **Dependencies:** `streamlit`, `pydantic`, `pyyaml` (see `requirements.txt`)
- **Data validation:** all domain objects are frozen Pydantic models, validated at
  load time, so a malformed scenario fails loudly at the boundary rather than
  producing a wrong schedule.
