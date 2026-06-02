# Architecture

This document explains how the Bus Charging Scheduler is built and *why*. It
covers the scheduling approach, the data model, the future changes the design
anticipates, and worked code examples for the two things the system is meant to
make trivial: **changing a weight** and **adding a rule**.

---

## 1. The problem in one line

Give each bus a **charging plan** (which of stations A, B, C, D it charges at) and
an **order** on each single charger, so that no bus ever exceeds its 240 km range
and the overall schedule is as "good" as possible under three **tunable weights**:
individual wait, operator fairness, and overall efficiency.

---

## 2. The scheduling approach (and why it fits)

### The shape of the problem

This is a **resource-constrained scheduling problem**: many jobs (buses) competing
for scarce machines (one charger per station) over time, judged by a weighted
objective. Problems of this shape are NP-hard in general, so there is no cheap
formula for the perfect answer — the realistic choice is between an exact solver
(slow, rigid) and a good heuristic (fast, flexible).

### What we chose: greedy + local search, behind a swappable interface

The engine has two cooperating heuristics:

1. **Greedy scheduler** — processes buses in arrival order; each bus picks the plan
   that yields the lowest *global* weighted cost given everyone placed so far, then
   commits. Fast and produces a strong first schedule.
2. **Local search** — starts from the greedy schedule and repeatedly tries
   "change one bus to a different feasible plan" moves, keeping any move that
   lowers the global cost, until no single move helps (a local optimum).

Both are wrapped behind a single `SchedulerStrategy` interface, so the algorithm
is a **swappable box** — you can pick one by name from a factory without touching
any caller.

### Why this is the right fit

- **It optimises the real objective.** Both heuristics score candidates with the
  *same* weighted cost function the final answer is judged by, so every weight
  steers every decision. There is no proxy metric to drift away from the goal.
- **It is fast and reproducible.** At the assignment's size (20 buses, 4 stations)
  it schedules in well under a second and returns the *same* schedule every run —
  important for a tool people must trust.
- **It bends to new rules without a rewrite.** Because the objective is a pluggable
  sum of rules (see §5), adding a goal never means re-deriving constraints. This is
  the decisive property for a system the brief says will keep growing.

### Why not an exact solver (CP-SAT / MILP)?

We *built* a CP-SAT model too and benchmarked it honestly. It can prove the optimum
only for very small instances; from ~10 buses up it can't prove within a 20-second
budget and was **2–40× worse** than the greedy, while being non-deterministic
(same input, different answers run to run) and front-loading a heavy "describe the
whole problem mathematically" build cost. Crucially, in a solver every new rule is
a new *constraint* to formulate — the opposite of the "add a rule without touching
the engine" property the assignment grades on. So CP-SAT is kept only as a
small-scale **optimality oracle** for testing, not as the production engine.

### Why not a discrete-event library (SimPy), DP, or genetic/ant-colony?

- **SimPy / DP** fight this model: the only shared resource is "one charger," times
  are deterministic, and the interesting part is *weight-driven arbitration*, which
  a plain simulation loop expresses far more clearly. Classic tabular DP needs a
  small bounded state, but the state here (charger occupancy over continuous time)
  is effectively unbounded.
- **Genetic / ant-colony** are heuristics in the same family as local search; they
  earn their keep at *thousands* of jobs, not twenty. Naming them and declining
  them with a reason is the right call at this size.

---

## 3. The data model

The guiding principle: **a scenario IS the data structure.** Each scenario is a
single self-describing file the engine reads; nothing about the world is hardcoded
in the code. Every fact lives on the entity that genuinely *owns* it, so growing
the world is a data edit, not a code change.

### The entities

```
Scenario(name, route, weights, trips[], reference_minutes)
│
├── Route(name, origin, destination, total_length_km, stops[])
│     └── RouteStop(station, position_km)        # position is route-RELATIVE
│           └── Station(name, chargers, charge_minutes)   # physical identity
│
├── Weights(values: dict[str, float])            # the tunable knobs, one map
│
└── Trip(bus, direction, departure_minute)       # one scheduled run (the input)
      └── Bus(bus_id, operator, range_km, speed_kmph)
```

The **output** of the engine is a parallel set of result objects:

```
ScheduleResult(scenario, trip_timelines[])
├── TripTimeline(trip, charge_events[], arrival_minute)   # one bus's journey
│     └── ChargeEvent(trip, station, arrival_minute, start_minute)  # one charge
└── station_schedules()  ->  StationSchedule(station, events[])     # per-station order
```

### Why ownership is split this way (the key modelling calls)

- **Direction is generic `FORWARD` / `REVERSE`, not city names.** The actual city
  names (`Bengaluru`, `Kochi`) live on the `Route` as `origin`/`destination`. This
  keeps the direction concept reusable across any future route.
- **Direction and departure live on `Trip`, not `Bus`.** A physical bus is reused;
  what has a direction and a departure time is a *run*. Separating the input
  timetable (`Trip`) from the physical vehicle (`Bus`) means the same bus can run a
  different way or time another day with no model change.
- **Physical constants live on the entity that owns them.** `range_km` and
  `speed_kmph` belong to the `Bus` (the vehicle); `charge_minutes` and `chargers`
  belong to the `Station` (the charger). Nothing physical leaks onto the scenario.
- **`position_km` lives on `RouteStop`, not `Station`.** "100 km from Bengaluru" is
  a property of the *(route, station)* relationship, not of the charger itself — so
  the same physical `Station` can sit at different positions on different routes.
- **Weights are a free-form `name → number` map.** A brand-new objective is just a
  new key; there is no fixed schema to migrate.
- **Time is relative integer minutes.** The engine works in minutes from a common
  reference (so all its arithmetic is trivial and shift-invariant). The scenario
  keeps the wall-clock anchor (`reference_minutes`, e.g. 19:00) purely so the UI
  can display real `HH:MM` times. The engine never parses dates.

### The scenario file format

Each scenario is one YAML file (readable, hand-editable, diffable). `ScenarioLoader`
is the single owner of the format: it parses the file, converts human `"HH:MM"`
times to relative minutes, validates everything, and returns frozen domain objects.
A malformed file fails **loudly at load time**, so every `Scenario` the engine sees
is already trustworthy.

```yaml
name: "Scenario 4 - Operator-heavy"
reference_time: "19:00"
route:
  name: "Bengaluru-Kochi Highway"
  origin: "Bengaluru"
  destination: "Kochi"
  total_length_km: 540
  stops:
    - { station: "A", position_km: 100 }
    - { station: "B", position_km: 220 }
    - { station: "C", position_km: 320 }
    - { station: "D", position_km: 440 }
weights:
  individual: 1.0
  operator: 2.0
  overall: 1.0
trips:
  - { bus_id: "bus-BK-01", operator: "kpn", direction: "forward", departure: "19:00" }
  # ... more trips ...
```

---

## 4. Anticipated changes — and how the design absorbs them *without code changes*

The brief asks specifically: *what will the next ask look like, and does your
design handle it as data?* Here is the list we designed for. In every row, the
change is **pure data** (edit a YAML file) unless explicitly noted.

| Future change | How the design handles it | Code change? |
|---|---|---|
| **Re-tune a weight** as field data arrives | Edit one number in the scenario's `weights` block | **None** |
| **Add a new scoring rule** (priority buses, electricity cost, driver shifts) | Write one `@register_rule` class; weight it in YAML | One small class, **no engine change** |
| **Mute an existing rule** for a scenario | Drop its key from `weights` (defaults to 0.0) | **None** |
| **More buses** per scenario | Add more `trips` entries | **None** |
| **More operators** | Use new operator names in `trips` | **None** (an enum entry if you want validation) |
| **A 5th station** / different positions | Add a `stops` entry with its `position_km` | **None** |
| **More than one charger** at a station | Set `chargers: 2` on that stop | **None** (simulator already supports N lanes) |
| **Longer-range or faster fleet** | Set `range_km` / `speed_kmph` per bus or via `fleet_defaults` | **None** |
| **A different / longer route** | Change `route` (stops, lengths, endpoint cities) | **None** |
| **Multiple routes sharing stations** | The same `Station` identity can appear on several routes via `RouteStop` | **None** for sharing; new route files are data |
| **A different charging duration** | `charge_minutes` per station | **None** |
| **A better algorithm** (CP-SAT, annealing, …) | Add a builder to the scheduler factory | One factory line, **no caller change** |
| **Per-operator priority on ties** | A pluggable simulator tie-break key | A small localized seam, **not an engine rewrite** |

The reason this list is mostly "None": every fact that might change lives on the
entity that owns it, and the two things most likely to change frequently —
**weights** and **rules** — are routed through a free-form weight map and a rule
registry, so they grow by configuration and registration rather than by editing the
engine.

---

## 5. How to change a weight (code example)

Weights live in **one place** — the `weights` block of a scenario file. The engine
reads them through `Weights.for_rule(key)`; nothing is hardcoded.

```yaml
# scenarios/scenario_4_operator_heavy.yaml
weights:
  individual: 1.0
  operator: 2.0     # raise to make operator fairness matter more
  overall: 1.0
```

What happens under the hood — the cost function is literally a weighted sum:

```python
# app/objective/cost.py  (simplified)
total_cost = 0.0
for rule in self._rules:                       # every registered rule
    raw_cost = rule.cost(context)              # the rule's badness, in minutes
    weight = self._weights.for_rule(rule.key)  # 0.0 if the key isn't set
    total_cost += raw_cost * weight
return total_cost
```

Because the weight is looked up by the rule's `key`, and a missing key returns
`0.0`, changing behaviour is exactly one value in one obvious place.

---

## 6. How to add a new rule (code example)

A rule is one scoring objective: given a finished schedule, return a non-negative
"badness" (0 = perfect). Adding one touches **no engine code**.

**Step 1 — define and register the rule:**

```python
# app/rules/builtin.py
from app.rules.base import Rule, RuleContext
from app.rules.registry import register_rule


@register_rule
class LateArrivalRule(Rule):
    """Penalise buses arriving after a target time."""

    @property
    def key(self) -> str:
        return "late_arrival"               # the weight key

    def cost(self, context: RuleContext) -> float:
        result = context.result             # the finished schedule
        target_minute = 300
        late = 0
        for timeline in result.trip_timelines:
            overshoot = timeline.arrival_minute - target_minute
            if overshoot > 0:
                late += overshoot
        return float(late)                  # 0 == perfect
```

**Step 2 — weight it in any scenario:**

```yaml
weights:
  individual: 1.0
  operator: 1.0
  overall: 1.0
  late_arrival: 3.0     # the new objective, switched on by data
```

Why nothing else changes: `@register_rule` adds the class to a shared registry,
and the cost function asks the registry for *every* rule and sums them. There is no
central list of rules to maintain and the schedulers never learn the rule's
internals — they only ever call `CostFunction.score(...)`.

```python
# app/rules/registry.py  (the seam)
@register_rule                       # decorator => registered at import time
class SomeRule(Rule): ...

# app/objective/cost.py
self._rules = get_registry().create_all()   # picks up the new rule automatically
```

---

## 7. The three built-in rules

| Rule | `key` | How it's measured (raw minutes) |
|---|---|---|
| Individual | `individual` | The **largest** single-bus wait (protect the worst-off bus) |
| Operator | `operator` | The **spread** (max − min) of total wait across operators (fairness) |
| Overall | `overall` | The **sum** of every bus's wait (system-wide efficiency) |

Each returns a raw number in minutes so the three are directly comparable and the
default weights of `1.0` are meaningful.

---

## 8. Hard rules the scheduler always respects

These are invariants, never traded off:

- A bus never travels more than **240 km** between charges (or origin→first charge,
  or last charge→destination). Infeasible plans are filtered out before scheduling.
- **One bus per charger** at a time (one charger per station, unless `chargers` says more).
- Charging is always exactly **25 minutes**, to full.
- A bus visits stations **in route order** — no backtracking.

A bus going the full 540 km must therefore charge **at least twice**; the scheduler
chooses which stations within the feasible set.

---

## 9. Assumptions made

The spec deliberately leaves gaps. The assumptions we made:

- **Speed = 60 km/h**, so travel minutes equal kilometres (a 100 km segment = 100
  min). Speed is a per-bus property, so this is easy to change.
- **Endpoints (Bengaluru, Kochi) are not scheduled** — buses leave fully charged and
  the slow endpoint chargers are out of scope, per the brief.
- **Charging is always to full**, even when a partial charge would do, per the spec.
- **Time is relative minutes** internally; the wall-clock anchor (`reference_time`)
  is kept only for display. Schedules are shift-invariant, so this never affects
  correctness.
- **Same-minute arrivals** at a charger are broken deterministically (by bus id)
  inside the simulator. This is a physical serve-order detail, kept out of the cost
  function; a per-operator priority discipline would be a small pluggable tie-break,
  not an engine change.
- **The greedy + local search find a strong local optimum, not a proven global
  one.** For this size that is the right trade (fast, reproducible, rule-flexible);
  the CP-SAT oracle exists only to bound the gap in testing.
- **The UI is read-only and shows one scenario at a time** — one dropdown, three
  views, no metrics dashboards — matching the brief exactly.

---

## 10. How the pieces fit together

```
scenarios/*.yaml
      │  ScenarioLoader  (app/io)         parse + validate + HH:MM → minutes
      ▼
   Scenario  (app/domain)                 frozen, trustworthy input
      │  create_scheduler()  (app/scheduling/factory)
      ▼
 SchedulerStrategy                        greedy seed → local search refine
      │   uses ScheduleSimulator (drive → wait → charge)
      │   scored by CostFunction = Σ weight[rule] × rule.cost   (app/objective + app/rules)
      ▼
 ScheduleResult  (app/domain)             per-bus timelines + per-station order
      │  Streamlit  (app/ui)
      ▼
   The three views in the browser
```

Every arrow is a clean seam: the loader owns the file format, the factory owns the
algorithm choice, the registry owns the set of rules, and the UI owns presentation.
None of them needs to know the internals of the others — which is exactly what lets
the world grow without rewrites.
