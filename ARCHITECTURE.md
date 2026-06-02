# Architecture & Decisions — Bus Charging Scheduler

This document records the design decisions made while building the scheduler, and
*why*. It is updated as we go, so the reasoning is captured while it is fresh.

## Problem in one line
Assign each bus a charging plan (which stations it charges at) and an order on each
single charger, so no bus exceeds its range and the schedule is "good" under tunable
weights (individual / operator / overall).

## Domain constants (assumptions)
- Route Bengaluru -> Kochi, stations A,B,C,D at km 100, 220, 320, 440; total 540 km.
- Battery range 240 km; charging 25 min, always to full.
- Speed assumed 60 km/h, so travel minutes == kilometres. (Our assumption; documented.)
- Endpoints (Bengaluru, Kochi) start full and are NOT scheduled.

## Decision 1 — Per-bus plan vs global schedule are SEPARATE layers
- A *plan* (which stations a bus charges at) is genuinely per-bus: it depends only on
  that bus's geometry + range. `feasible_plans()` enumerates the legal menu.
- The *schedule* (all buses placed on shared chargers over time) is global: one bus
  charging at B changes everyone else's options at B.
- The metrics we optimize (total wait, worst wait, per-operator wait) are functions of
  the GLOBAL schedule, not of any single bus.

## Decision 2 — Four independent concerns (so we can swap one without rewriting others)
1. Plan generation (`feasible_plans`) — geometry + range. Never changes as world grows.
2. Simulator (drive -> wait -> charge) — how we EVALUATE any candidate schedule.
3. Objective + weights — turn schedule facts into one cost; weights in ONE place.
4. Search strategy — HOW we explore which combo of plans to commit. This is the only
   box that changes when we upgrade greedy -> local search -> CP-SAT.

## Decision 3 — Scheduling approach: greedy baseline, then local search, (optional) CP-SAT
- Current engine is a GREEDY heuristic: process buses in departure order, each picks its
  own lowest-cost plan given buses already placed, then commits (blocks chargers).
- Known limitation (important, defensible): greedy optimizes locally/sequentially and is
  NOT globally optimal. It cannot make a "sacrifice one bus now for bigger collective
  gain" move, because later buses don't exist in an early bus's view.
- Plan: add a global cost function + a LOCAL SEARCH layer (start from greedy, try plan
  swaps, keep swaps that lower global cost). Escapes the greedy trap, uses same simulator.
- For PROVABLE global optimum we would use CP-SAT / MILP (industrial branch-and-bound).
  Deferred because 20 buses don't need it and it adds a dependency + makes "add a rule
  live" become "add a constraint". We will compare results + cost against greedy/local.

## Decision 4 — Why NOT classic tabular DP for the global problem
- DP needs a small bounded state with overlapping subproblems (Markov property).
- Here the state that summarizes the past is the FULL charger-occupancy over time
  (busy intervals + gaps on 4 machines), which is effectively continuous/unbounded and
  almost never shared between histories -> no overlapping subproblems -> DP degenerates
  into brute force. This is a resource-constrained job-shop problem (NP-hard).
- DP DOES apply to the single-bus subproblem (shortest path / reachability under range);
  `feasible_plans()` is a baby version of that.

## Decision 5 — Why not SimPy
- Only shared resource is "1 charger per station"; deterministic times. The interesting
  part is weight-driven arbitration, which SimPy's model fights. Plain loop keeps the
  arbitration explicit and easy to modify live. SimPy would pay off only for stochastic,
  multi-resource, preemptive queueing — not this.
- "N chargers per station" is a small change either way (list of N free-times).

## Decision 6 — Weights live in ONE place (`WEIGHTS` dict, later from scenario file)
- Changing behaviour = change one value. Each soft rule is an additive term in the cost.
- KNOWN SMELL to fix: current `overall` term reuses `predicted_arrival`, making it
  collinear with `individual_arrival`. A real `overall` metric needs a FULL schedule to
  measure (total arrival / makespan) — which arrives with the global cost function.

## Data format (planned)
- Scenarios as YAML files (readable, hand-editable, comments). JSON is the fallback.

## Build progress (function by function, in main.py)
- STEP 1 `feasible_plans()` — range-valid charge station subsets. DONE.
- STEP 2 `build_timeline()` — one bus, one plan, no contention. DONE.
- STEP 3 `schedule_buses()` — many buses, 1 charger/station, waiting. DONE.
- STEP 4 `choose_best_plan()` — one bus picks earliest-arrival plan (look only). DONE.
- STEP 5 `run_scheduler()` — full greedy (choose + commit per bus). DONE.
- STEP 6 `score_plan()` / `choose_best_plan_weighted()` — weighted cost selection. DONE.
- NEXT — global cost + local search (plan swaps); later CP-SAT comparison.

## Decision 7 — Local search: how it starts, moves, and stops
- INITIAL assignment = the greedy schedule's output (each bus's chosen plan). Greedy is a
  good seed (already near-good), so fewer swaps are needed than from a random start.
- A "neighbor" = the same assignment with ONE bus changed to one of its OTHER feasible
  plans (everyone else fixed). This is a "1-swap" move.
- We do NOT enumerate the whole space. Two very different sizes:
    * whole space   = plans ^ buses        (exponential, ~3^20 ≈ 3.5e9, hopeless)
    * neighborhood  = buses * (plans - 1)  (LINEAR, ~40 per step, cheap)
  Local search lives in the linear world; that is the whole point.
- STOP rule = local minimum: a full pass over all neighbors finds no improver.
- Caveat: a local minimum is NOT guaranteed global. Escapes (in order of power):
  random restarts -> larger neighborhood (2-swap) -> simulated annealing / tabu -> CP-SAT.

## Decision 7b — Why local search NEEDS the while loop (not a single pass)
- One pass (just the for-loop) finds the best SINGLE swap from the CURRENT assignment.
  After applying it you are at a DIFFERENT assignment with NEW neighbors you never looked
  at -- some swaps only become improvements AFTER an earlier swap, because buses interact
  on shared chargers.
- Example: start cost 100. Pass 1 best move (swap bus-02) -> 80; at that moment swapping
  bus-04 looked WORSE (110) so it was correctly rejected. Only AFTER bus-02 moved does
  swapping bus-04 help: pass 2 -> 65. Pass 3 finds nothing -> stop. A single for-loop would
  have stopped at 80 and missed 65.
- Mental model: walking downhill in fog. The for-loop = "which direction is most downhill
  from where I stand?". After stepping you must RE-LOOK from the new spot. Stop only when
  EVERY direction goes up (the local minimum). The re-looking IS the while loop.
- Termination is guaranteed: each pass applies only a STRICT improver, so cost strictly
  decreases and is bounded below -> a bounded (small, in practice handful) number of passes.
  Total work = passes * (linear ~40 neighbors), still nowhere near the exponential space.
- A single pass is a valid but WEAKER algorithm ("single-step / no iteration"): faster,
  stops early at a worse schedule. The while loop is the difference between "take one good
  step" and "walk to the bottom of the valley". (Still only a LOCAL bottom -> CP-SAT next.)

## Decision 8 — Why NOT genetic algorithm / ant colony (for now)
- GA/ACO are metaheuristics in the same family as local search: explore a huge space
  without enumerating it, trading provable-optimal for fast-and-good.
- GA maps cleanly here (chromosome = the bus->plan dict, gene = one bus's plan,
  fitness = global_cost, crossover = mix two parents' plans, mutation = reassign one bus).
- ACO maps awkwardly: it is built for path-CONSTRUCTION problems (TSP/routing) where a
  solution is a path with pheromone trails; our solution is an assignment, not a path.
- DECISION: skip both. They earn their keep at THOUSANDS of buses where CP-SAT chokes.
  At 20 buses CP-SAT solves to optimum faster than a GA can be tuned. Naming them and
  declining them with a reason is the right engineering judgment.

## Decision 9 — Future plan-menu pruning (cost-pruning), NOT yet implemented
- Idea: attack the BASE of the exponent (plans-per-bus), the highest-leverage lever:
  cutting 3 -> 2 plans turns 3^20 into 2^20 (~3000x smaller). Helps greedy, local search,
  AND CP-SAT simultaneously.
- `feasible_plans()` already does RANGE pruning (drops illegal plans). This is a SECOND
  stage: COST pruning that drops legal-but-bad plans. Pipeline:
      all plans --range filter--> feasible --top-k cost filter--> candidate menu
- Two flavors:
    * hard pruning: keep only min-charge-count / dominated-removed plans. RISK: a plan
      that is dominated in ISOLATION can be the swap that dodges a crowded charger under
      contention -> hard pruning can amputate the moves local search needs.
    * soft pruning (preferred): rank each bus's feasible plans by solo cost, keep top-k.
      Caps the exponent base at k while keeping contention-dodging flexibility. This is
      the "top-k shortest paths" idea.
- Deferred: only matters once bus/plan counts get large.

## Decision 10 — Why "just use CP-SAT" is wrong: model-build cost + rule rigidity
A solver call has TWO phases, and people forget the first:
  BUILD  = translate the problem into the solver's math (create every variable +
           constraint). This happens BEFORE any searching.
  SOLVE  = branch-and-bound searches the built model for the optimum.
Our model creates, per (bus, station): a bool charges_here, int start/end/wait, an
optional interval, several constraints, plus one cumulative constraint per station over
ALL buses. At 1000 buses * 12 stations that is hundreds of thousands of objects -> the
BUILD alone took ~2.1s (vs 2ms at tiny size), and that time is SUBTRACTED from the solve
budget. Greedy has NO build phase; it walks buses directly. So declarative solvers
front-load a heavy "describe everything mathematically" tax that imperative heuristics
skip entirely.

Rule flexibility (the spec's "adding a rule must not rewrite the engine"):
  - Weighted simulator: a rule is an ADDITIVE term. e.g. "KPN must not wait > 10 min" =
    `if operator=='KPN': cost += BIG * max(0, wait-10)`. One function touched; engine,
    search loop, and other rules unchanged. Fuzzy/fairness rules are natural (any Python).
  - CP-SAT: the SAME rule is a MODELING task -- express it as a linear constraint over
    integer vars, often needing auxiliary vars + reification (only_enforce_if) for soft
    versions, and it can interact with the solver's pruning. Some rules (if-then-else,
    ratios, "smooth out an operator's fleet") are hard or impossible to linearize.

## Decision 11 — Benchmark findings (benchmark_cpsat.py -> benchmark_results.txt)
Setup: synthetic worlds, route 540 km, range 240 km, departure gap 8 min (tight =>
contention), 1 charger/station, CP-SAT capped at 20s with 8 workers; build time and solve
time measured separately. Three engines on identical instances.

IMPORTANT — earlier numbers in this section were on a BUGGY CP-SAT model and have been
replaced. Two bugs were found and fixed (see Decision 11b): CP-SAT was solving a physically
WRONG, easier relaxation, so its old objectives (e.g. "obj 74" / "obj 4") were impossibly
low. Corrected numbers below.

Corrected key numbers (honest model, CP-SAT now an exact oracle on what it can prove):
  tiny         3/4 : all three = 9, CP-SAT PROVES optimal in 45ms. Greedy == optimum.
               6/4 : all three = 54, CP-SAT PROVES optimal in 107ms. Greedy == optimum.
  small/hard   10/4: CP-SAT 180 (FEASIBLE, NOT proven, full 20s) == greedy 180.
               20/4: CP-SAT 995 (not proven) vs greedy 810  -> GREEDY 19% BETTER.
               40/4: CP-SAT 6756            vs greedy 3420   -> GREEDY ~2x better.
               20/6: CP-SAT 275             vs greedy 57     -> GREEDY ~5x better.
               40/6: CP-SAT 6467            vs greedy 247    -> GREEDY ~26x better.
               60/6: CP-SAT 22566           vs greedy 570    -> GREEDY ~40x better.
               80/8 & 120/8: greedy hits 0 (perfect); CP-SAT stuck at 57k / 117k.
  scale 1000   1000/4 : CP-SAT UNKNOWN (NO feasible answer in 20s) vs greedy 2.245M.
               1000/6 : CP-SAT UNKNOWN                              vs greedy 166k.
               1000/8 : CP-SAT UNKNOWN                              vs greedy 0 (perfect).
               1000/12: CP-SAT UNKNOWN                              vs greedy 165k.
  speed        greedy answers in ms-to-seconds on every row; CP-SAT burns the full 20s on
               every row above 6 buses and STILL loses.
  build cost   cpsat_build_s grows 3ms -> 9.4s at 1000/12, eating most of the solve budget
               before the solver even starts.
Conclusions (the headline of this whole exercise — NOW with an honest model):
  1. The realistic model (true precedence + range) is FAR harder than the buggy relaxation.
     CP-SAT only wins (proves optimum) on TINY instances (<= 6 buses here).
  2. Everywhere else, capped CP-SAT is BOTH slower AND worse than greedy -- often by 2-40x,
     and at 1000 buses it returns NO feasible answer at all while greedy answers in ms.
  3. So "just use CP-SAT" is wrong for this problem at any realistic size. A cheap, correct
     greedy heuristic dominates on the speed/quality/robustness front.
  4. This is exactly why search strategy is a SWAPPABLE box (Decision 2): CP-SAT for tiny
     instances where a PROOF of optimality matters; greedy/local for everything real.
  5. Lesson on optimization itself: an optimizer is only as trustworthy as its model. The
     buggy CP-SAT looked like a clean winner (obj 4 vs 54) precisely because it was cheating
     physics. Always validate the solver's chosen schedule against the simulator.
NOTE: local search still == greedy on every row. CONFIRMED BENIGN (not a bug): brute force
shows greedy already reaches the GLOBAL optimum of the simulator model on these symmetric
worlds (6/4 exhaustive optimum = 54 = greedy; on 20/4 zero 1-swap AND zero 2-swap improvers
exist). Equal, equally-spaced buses make greedy's even split already optimal.

## Decision 11b — Two CP-SAT modelling bugs found while validating the benchmark
While checking why CP-SAT reported impossibly-low waits, two physical-correctness bugs were
found in solve_with_cpsat / add_range_feasibility and fixed:
  1. PRECEDENCE / charge-time leak: arrival at a station was hard-coded `departure + km`,
     ignoring the 25-min charge + any wait already spent at EARLIER stations. Each charge
     floated independently, so charges packed into chargers with almost no overlap -> fake
     low wait (obj 4 vs true 54 on 6 buses). FIX: arrival is now a VARIABLE,
     arrival = departure + km + sum(consumed at earlier chosen stations), where consumed =
     wait + 25 if it charges there else 0; plus start >= arrival, and start == arrival when
     not charging so nothing floats.
  2. RANGE leak: add_range_feasibility counted the gap ENDPOINTS as "covering" stations
     (range(earlier, later+1)), so an over-range pair like charge-at-st0-then-st3 was
     trivially "covered" by st0 and st3 themselves -- yet st0->st3 = 324 km > 240 range is
     illegal. FIX: covering set is now STRICTLY between (range(earlier+1, later)); when no
     bridging station exists, forbid choosing both endpoints (or the lone station endpoint).
After both fixes: 6-bus case CP-SAT = greedy = exhaustive true optimum = 54, proven optimal,
every plan range-valid, no charger overlaps. CP-SAT is now an honest optimality oracle.

## Decision 12 — FINAL JUDGEMENT: greedy is the engine we ship (CP-SAT stays as oracle)
Having built BOTH engines honestly and benchmarked them on identical worlds, the decision is
greedy (event-driven simulation + pluggable weighted rules) as the production engine, with
CP-SAT kept only as a small-instance optimality ORACLE / test oracle. Reasons, in order of
weight:

1. QUALITY at real size: on the honest model CP-SAT only PROVES the optimum at <= 6 buses.
   From 10 buses up it can't prove in 20s and is 2-40x WORSE than greedy (40/6: 7818 vs 247;
   60/6: 26913 vs 570). At 1000 buses it returns UNKNOWN (no feasible answer at all) on every
   station count while greedy answers in milliseconds. The cheap heuristic dominates.

2. SPEED: greedy/local stay in ms-to-seconds on every row; CP-SAT burns the full 20s budget
   on every row above 6 buses and STILL loses. Build time alone grows to ~3-9s at 1000 buses,
   eating the solve budget before search starts.

3. REPRODUCIBILITY: capped CP-SAT with 8 workers is NON-deterministic -- the same 20/6 case
   returned 275 on one run and 1113 on the next (a 4x swing). Greedy returns the identical
   schedule every run. A scheduler people must trust should be reproducible.

4. RULE FLEXIBILITY (the decisive one for this assignment, which explicitly grades on adding
   rules / tunable weights):
   - Tunable WEIGHTS: BOTH engines support these. CP-SAT's objective IS a weighted linear sum
     (min sum of w_k * term_k), so changing a weight is changing one coefficient -- as easy as
     greedy's weight dict. So "tunable weights" alone does NOT favour either engine.
   - The real gap is the KIND of rule you can attach a weight to. CP-SAT only accepts rules
     expressible as LINEAR INTEGER (in)equalities over its decision variables. Anything else
     must be hand-translated:
       * hard threshold ("KPN never waits > 10 min")  -> reify a bool + big-M linearization
       * nonlinear penalty ("cost grows with wait^2") -> impossible directly; piecewise-linear
         approximation only
       * conditional logic ("if late AND premium then reroute") -> boolean implications +
         indicator constraints
       * stateful / external rule ("favour whoever waited most last scenario") -> impossible;
         no arbitrary Python at solve time
     In greedy each of these is ONE Python scoring function with full access to if/else,
     nonlinearity, state and external data -- and you can print() it to verify. A new rule =
     add a function + a weight key (Decision 4 registry). No re-modelling, no big-M, no risk
     of a wrong linearization silently changing the physics (exactly the bug class that made
     CP-SAT cheat in Decision 11b).

5. EXTENSIBILITY architecture (Decision 2): search is a swappable box, so this is not a
   one-way door -- CP-SAT can be re-selected per instance. We keep it wired in as: (a) a proof
   oracle on tiny instances, and (b) a TEST oracle -- on small cases greedy's result must equal
   CP-SAT's proven optimum (6/4 = 54 on both), which is a cheap automated correctness check on
   the greedy engine.

Bottom line: greedy wins on speed, quality, robustness AND rule flexibility for every
realistic size of this problem. CP-SAT earns its keep only as a small-scale oracle. Meta-
lesson for the interview: an optimizer is only as trustworthy as its model; the "obvious"
heavy solver looked like a winner only while it was secretly solving an easier problem.

## Decision 13 — Production domain model: correct ownership of properties (entities)
The learning script (`main.py`) used a flat, assignment-shaped model. While rebuilding the
production `app/` package we re-examined WHICH entity each fact really belongs to, so the
model scales to many routes and turns several "anticipated changes" into config-only edits.

13a. Direction is ROUTE-AGNOSTIC, not a pair of hardcoded city names.
  - OLD: `Direction.BENGALURU_TO_KOCHI` / `KOCHI_TO_BENGALURU`. This does not scale: with a
    second route the enum is meaningless ("which BK?"), and every new route would need new
    enum members + code.
  - NEW: `Direction.FORWARD` (origin -> destination) and `Direction.REVERSE` (destination ->
    origin). The CITY NAMES live on the Route (`origin`, `destination`), not in a global enum.
    100 routes => still exactly two Direction values; each route names its own endpoints.
  - `station_sequence(direction)` is unchanged in spirit: FORWARD walks stations in stored
    order with distance == position_km; REVERSE walks them reversed with distance ==
    total_length - position_km.

13b. Direction + departure belong to a TRIP, not to a Bus.
  - A physical Bus is reused across runs; what has a direction and a departure time is a
    scheduled RUN of a bus on a route. So we introduce a `Trip` entity = (bus, direction,
    departure_minute). `Bus` LOSES `direction` and `departure_minute`.
  - `departure_minute` is INPUT-timetable data (the given question), conceptually distinct
    from our OUTPUT charging schedule. `Trip` is that input timetable entry; the charging
    StationSchedule we PRODUCE is a separate (later) entity. Naming them apart avoids the
    "two different schedules" confusion the assignment can cause.

13c. Physical constants move to the entity that actually owns them.
  - `range_km`  -> Bus     (battery capacity is the vehicle's property).
  - `speed_kmph`-> Bus     (the vehicle's cruising speed; a route may cap it, noted below).
  - `charge_minutes` -> Station (the charger's fill time / capacity of the charger).
  - Previously ALL THREE sat on `Scenario` as a shortcut. Correct ownership is the payoff:
    "make bus 7 long-range" or "this station has fast chargers" become pure DATA edits with
    NO code change -- a direct hit on the README's "anticipated changes handled without code".
  - `Bus.travel_minutes_for_km(km)` moves onto Bus (uses the bus's own speed).
  - Refinement noted, NOT built (avoid over-engineering): true charge time = battery / charger
    power, i.e. a bus x station interaction; and a route could impose a speed cap. We model
    charge_minutes as a flat Station property (uniform 25 min in the assignment) and record
    the richer physics as a future change rather than coding it now.

13d. Shared charger is contended from BOTH directions; order by ARRIVAL, not input order.
  - One physical charger per station is used by FORWARD and REVERSE trips alike. At each
    station we MERGE trips from both directions and process them by ARRIVAL TIME at that
    station (arrival = departure + travel + any earlier charge/wait), NOT by the order the
    scenario lists them (the assignment lists one origin's buses, then the other's). The
    timetable, not the file order, drives charger arbitration. (Refines Decision 11's
    "greedy order = departure time" to "arrival time at each contended station".)

13e. Bus is NOT linked directly to a Route.
  - A bus is not bound to a road; a TRIP places a bus on a route+direction. The Route stays at
    `Scenario` level (one shared road for all trips in a scenario), which matches the
    assignment and keeps the model clean. If multi-route scenarios are needed later, the route
    reference can move onto `Trip` without disturbing Bus or Station.

Resulting entity shape:
  Route(name, origin, destination, total_length_km, stations[])    # cities live here
  Station(name, position_km, chargers, charge_minutes)             # charger owns fill time
  Bus(bus_id, operator, range_km=240, speed_kmph=60)               # vehicle physics only
  Trip(bus, direction, departure_minute)                           # input timetable entry
  Scenario(name, route, weights, trips[])                          # the whole situation
(NOTE: superseded by Decision 14 -- position_km moves off Station onto RouteStop.)

## Decision 14 — Station identity vs. route-relative position (RouteStop)
A station's `position_km` is NOT a property of the station; it is a property of the
(route, station) RELATIONSHIP. A physical charger has no notion of "distance from
Bengaluru" -- that only has meaning relative to a particular route. So we split:
  * `Station`   -- pure physical IDENTITY + charger physics: name, chargers, charge_minutes.
                   It carries NO position.
  * `RouteStop` -- the association value object = (station, position_km). The position lives
                   HERE, on the route-relative link.
  * `Route`     -- now holds `stops: tuple[RouteStop, ...]` instead of bare stations.

Why this is the right design (and why the tempting alternative is wrong):
  - SHARED stations across routes (the long-term growth case): the SAME `Station` object can
    be referenced by `RouteStop`s in two different routes, each with its own `position_km`.
    No duplication, no inconsistency.
  - REJECTED alternative -- storing a `{origin: distance}` dict on the Station. That inverts
    the dependency: a station would have to be edited whenever ANY route is added, two routes
    could write conflicting distances, and the value is already DERIVABLE. "Distance from a
    given origin" is answered by asking the relevant ROUTE (`station_sequence(direction)`),
    not by the station carrying route knowledge.
  - `charge_minutes` and `chargers` stay on `Station` -- they ARE intrinsic to the physical
    charger (identity), unlike position which is relational.
We deliberately stop here: no geo-coordinates, no global station registry. Those would be
over-engineering for a single-route assignment; the entity/association split is the minimal
change that models reality correctly and unlocks shared stations later as pure data.

## Decision 15 — Time representation: relative minutes now, anchor deferred to the loader
The scheduler is SHIFT-INVARIANT in time: every quantity it computes (wait = arrival - ready,
makespan = last - first) is a DIFFERENCE, so any common time anchor CANCELS OUT. Epoch-minutes
and minutes-from-zero therefore produce IDENTICAL schedules -- the choice is purely about
readability and test determinism, not correctness.
  - DECISION (now): keep `departure_minute` as small integer minutes from 0 (0 == the
    scenario's reference instant). Readable logs, deterministic tests.
  - REJECTED (for now): epoch-minutes smeared across every field -> 8-digit numbers in every
    DEBUG log, and "add today's date" makes tests non-deterministic.
  - PLANNED evolution (at the loader step, a BOUNDARY concern): store ONE `reference_time`
    anchor (a real date/epoch) on the Scenario; the loader parses a full timestamp into an
    offset, or combines a bare "HH:MM" with the scenario's date. Wall-clock is then fully
    recoverable (wall = anchor + offset) WITHOUT polluting the domain math. The engine never
    parses dates.

## Decision 16 — Same-arrival tie-break is a SIMULATOR concern, not a cost/weight concern
When two buses arrive at the SAME station at the SAME minute, SOMETHING must decide who takes
the single lane first. This is a property of the charger's serve discipline (who is physically
served), and it lives ENTIRELY in the simulator -- specifically in the ordering key of the
event heap. It is NOT, and cannot be, a job for the rule registry / global cost function.
  - WHY NOT THE COST FUNCTION: the cost function runs AFTER the simulator has already produced a
    ScheduleResult. It only SCORES a finished schedule (to choose which plans win in the greedy
    loop); it has no lever to reorder who got a lane during simulation. So "serve KPN first on a
    tie" is unreachable from cost -- it must be encoded in the simulator's serve order.
  - TWO DIFFERENT "PRIORITIES", TWO DIFFERENT LAYERS (the key distinction to state in interview):
      * "On an exact tie, serve operator X's bus first" -> SIMULATOR tie-break (heap key).
      * "Operator X should wait less ON AVERAGE / be balanced vs others" -> COST layer, via the
        operator-fairness rule + its weight (Decision: `operator` weight, e.g. scenario 4 = 2.0).
        Note that `operator` weight is a SINGLE SYMMETRIC knob (balance among operators); it does
        NOT favour a specific operator. Per-operator priority would be added as config + one rule
        (e.g. `operator_priority_<name>` weight keys + an OperatorPriorityRule) -- no engine change.
  - CURRENT STATE: the heap key is `(arrival_minute, bus_id, trip_index)`, so ties break by
    bus id (alphabetical) then trip index -- deterministic but ARBITRARY (no policy meaning).
  - PLANNED extension (NOT built now, to avoid over-engineering): make the tie-break a pluggable
    key / small Strategy on the simulator -- e.g. `(arrival_minute, operator_rank, bus_id)` for an
    operator-priority discipline -- swappable WITHOUT touching the event loop (open/closed). We
    record it here as a clean, localized extension point rather than coding a policy the spec
    does not ask for (the assignment gives one charger, uniform buses, no stated tie policy).

## Decision 17 — Globally-scored greedy over selfish greedy (design "1A"), and how it scales
The greedy chooses each trip's plan by RE-SIMULATING the schedule-so-far (already-committed trips
plus the candidate) and scoring it with the SAME global weighted cost function the final answer is
judged by -- it commits the candidate with the lowest GLOBAL cost. The rejected alternative is
`main.py`'s "selfish" greedy, where each bus minimised only its OWN predicted arrival time.

WHY GLOBAL-SCORED IS BETTER (three reasons):
  - OPTIMISES THE RIGHT THING. The assignment's quality metric IS the weighted global cost
    (individual worst-wait + operator fairness + overall wait). Selfish greedy optimises a PROXY
    (own arrival) that can diverge arbitrarily from that metric. Two of the three objectives --
    operator fairness and overall wait -- are properties of the WHOLE schedule, so a bus that sees
    only itself is structurally blind to them. Observed in Step 7: bus-G2's selfishly-fine plan
    (A,C) scored global cost 60 (it forced a later bus to wait), so the greedy instead took (B,D)
    at cost 0 -- it spread load to avoid contention with NO contention-specific code.
  - WEIGHT-DRIVEN, ZERO CODE COUPLING. The greedy only ever calls `CostFunction.score()`; it holds
    no objective knowledge. Retuning a weight or `@register_rule`-ing a new objective changes its
    decisions with no change to the scheduler. Selfish greedy hard-bakes "minimise my arrival", so a
    new objective would mean rewriting the search.
  - CANNOT BE LOCALLY-GOOD-BUT-GLOBALLY-BAD per step. Because each commit is judged by the real
    objective over everyone-so-far, the greedy never makes a choice that looks good for one bus but
    hurts the actual score (within its one-pass horizon).

COST / SCALING (honest bounds):
  - Complexity ~ O(trips x plans_per_trip x simulation_cost). For THIS assignment (<=20 trips,
    <=8 feasible plans/trip, ~4 stations) that is a few hundred fast simulations -- negligible.
  - Scales fine for this CLASS (tens of trips, a handful of stations). It does NOT scale to large
    fleets / long routes: feasible plans grow combinatorially with station count (subsets), and
    full re-simulation per candidate is wasteful versus an incremental delta. If forced to scale:
    (a) score INCREMENTALLY instead of re-simulating the whole schedule, (b) PRUNE dominated plans,
    (c) fall back to the CP-SAT formulation for exactness. We deliberately keep the simple, fully
    re-simulating version here: it is far clearer, reuses one trusted simulator + cost function, and
    premature optimisation is unwarranted at the assignment's size.
  - STILL GREEDY: one forward pass, commits are permanent. Good and fast, not guaranteed optimal --
    which is exactly why local search (refines by revisiting commits) and the CP-SAT oracle
    (bounds the optimality gap in the benchmark) follow.

### Decision 18 - Two greedy SEED variants: global-cost vs selfish-arrival
We keep TWO greedy schedulers behind the same `SchedulerStrategy`, differing ONLY in how a
candidate plan is scored. They share one commit-loop skeleton (`_GreedyBase`: deterministic trip
order + commit-one-trip-at-a-time) and fill in just the per-candidate scoring hole.

- `GlobalGreedyScheduler` (the "global" lens, design 1A): scores a candidate by RE-SIMULATING the
  whole schedule-so-far and taking the SAME weighted global cost the final answer is judged by.
  Every weight steers every decision; cost is ~O(trips^2 x plans x sim) -- quadratic but high
  quality.
- `SelfishGreedyScheduler` (the "selfish" lens, faithful to `main.py`'s `choose_best_plan`): scores
  a candidate by THIS trip's OWN predicted arrival, read off an incrementally-maintained set of
  `ChargerStation`s via a new non-mutating `ChargerStation.peek` (the read-only twin of `request`).
  No whole-schedule re-simulation; cost is ~O(trips x plans x plan_length) -- roughly linear and far
  cheaper at scale, but blind to operator fairness / overall wait, so seed quality is lower.

KEY SAFETY: the selfish charger bookkeeping is an APPROXIMATION (it advances chargers in commit
order, not true arrival order, like `main.py` did). That only affects which plans are CHOSEN. The
FINAL `ScheduleResult` is always produced by the real arrival-order `ScheduleSimulator` in the base
`schedule` template, so the reported schedule is always exact regardless of which seed picked it.

WHY KEEP BOTH: they are SEEDS for the upcoming local search. Keeping both lets us benchmark seed
quality vs speed (global = better start, slower; selfish = faster start, scales) once the pipeline is
complete -- the planned performance comparison. Pluggability is free: both are drop-in
`SchedulerStrategy`s, and a `SchedulerFactory` can later expose them by name.

### Decision 19 - Local search REFINES a greedy seed by single-trip plan swaps
`LocalSearchScheduler` is another `SchedulerStrategy`: it takes a SEED schedule from a greedy
strategy (`SelfishGreedyScheduler` by default, pluggable to the global greedy later) and improves it
by steepest-descent hill climbing. Each round it scores every "change ONE trip to a different
feasible plan" neighbour by re-simulating the WHOLE schedule and re-scoring with the SAME global
cost function, applies the single most-improving swap, and repeats until no single swap lowers the
cost (a local optimum) or a `max_iterations` cap is hit (always terminates).

WHY THIS MATTERS: a greedy commits once and never looks back, so it can stop just short of a better
arrangement (two trips that would each be happier swapped, but neither swap looked best at its own
commit moment). Local search closes exactly that gap. Demonstrated: with operator fairness weighted
up, the fairness-BLIND selfish seed lands at cost 125; local search makes one swap (bus-L1 -> B,C)
to rebalance operators and reaches cost 75, then a local optimum.

DESIGN: seed is obtained by calling the seed strategy's own `_choose_plans` with the shared
tools, so the seed behaves identically to running alone; local search only takes over afterwards.
It reuses the trusted simulator + cost function unchanged, so it always optimises the REAL objective
(every weight still steers it). It is a HEURISTIC -- local, not global, optimum -- which is why the
CP-SAT oracle in the benchmark exists to bound the gap. Steepest descent (best neighbour per round,
ties keep first-found in scenario order) is chosen over first-improvement for determinism and
clearer logs at this size.

SCALING: each round is O(trips x plans x sim_cost) and rounds are bounded by the cap; fine for the
assignment. At large scale this would want incremental re-scoring / restricted neighbourhoods, same
caveat as the global greedy (Decision 17/18).

### Decision 20 - A NAME->strategy factory; default = local search; UI has NO strategy picker
`app/scheduling/factory.py` exposes `create_scheduler(name="local_search")` over a small name->builder
registry (`selfish_greedy`, `global_greedy`, `local_search`). Every caller -- CLI, Streamlit UI,
tests -- asks the factory for "a scheduler" instead of importing a concrete class, so the algorithm
choice lives in ONE obvious place and swapping it is a one-liner (the flexibility we demo live).
Builders (not instances) are stored so each call yields a fresh, independent scheduler and
construction stays lazy. Adding a strategy later = one builder + one registry entry; no caller
changes.

DEFAULT is `local_search` (selfish-greedy seed + hill climb): the highest-quality option, and the
one the UI uses. IMPORTANT (matches the spec): the assignment's UI asks for ONE dropdown -- to pick a
SCENARIO -- and explicitly "no metrics dashboards". So the multiple strategies are an INTERNAL
architecture asset (Strategy + Factory, good to defend in the interview), NOT a UI feature; the UI
will not expose a strategy selector. The factory is just the clean wiring seam the UI/CLI use to
construct the single configured scheduler.

### Decision 21 - A scenario is ONE self-describing YAML file; loader owns the format
Each of the 5 scenarios ships as a standalone `scenarios/*.yaml` that fully describes the situation:
route + stops (each station's name/position/chargers/charge_minutes), the tunable `weights` map, an
optional `fleet_defaults` (range/speed), and the `trips` timetable. `app/io/loader.py`
(`ScenarioLoader`) is the SINGLE owner of the file format: `load_file`/`load_dir` parse the YAML and
return fully-validated frozen `Scenario` objects, so the engine and UI never touch YAML.

KEY CHOICES:
- SELF-DESCRIBING & self-contained: the route is embedded in every file (not referenced), so a file
  can be shipped/edited/diffed alone. The small duplication is deliberate -- a scenario IS the data
  structure (per the brief), and a future scenario can change the route, add a 5th station, give a
  station 2 chargers, or model a longer-range fleet purely by editing data, with ZERO code change.
- CLOCK TIMES at the boundary only: files use human `"HH:MM"` departures plus a `reference_time`
  anchor; the loader converts to the domain's relative `departure_minute` (Decision 15). The domain
  models only ever see integer minutes; the UI converts back for display.
- WEIGHTS as a free-form map: new rule => new weight key in the YAML, no schema change (mirrors the
  `Weights.values` design). Scenario 4 simply sets `operator: 2.0`.
- FAIL LOUD at load: malformed files raise here (bad time, stops out of order via the Route
  validator, unknown operator), so every `Scenario` handed downstream is already trustworthy.
- direction in files is `forward` (Bengaluru->Kochi) / `reverse` (Kochi->Bengaluru); the city names
  live on the route, keeping the enum route-agnostic (Decision 13a).

### Decision 22 - Local search seeds from the GLOBAL greedy (benchmark-driven default)
A throwaway `benchmark.py` runs every strategy on every scenario and records cost/wait/time. Two
things came out of it and changed the design:

1. THE RULE REGISTRY MUST SELF-POPULATE. The registry only knows a rule once its module is imported
   (the `@register_rule` side effect). The benchmark imported neither `app.rules.builtin` nor went
   through the rules demo, so it ran with an EMPTY registry and every schedule scored a meaningless
   `0.0`. Fix: `app/rules/__init__.py` now imports `builtin` for its side effect, so importing the
   rules package self-registers the built-ins. Any registry consumer (cost function, schedulers,
   benchmark, UI) is now correct without remembering to import `builtin`. This is the standard
   plugin-registration pattern and removes a real footgun.

2. THE LOCAL-SEARCH SEED CHANGED selfish -> global. With a fair objective the selfish-greedy seed
   left local search stuck in a worse local optimum than the global greedy reached outright on 3 of
   5 scenarios. Re-seeding local search from the GLOBAL greedy makes it start from the stronger
   schedule and only ever improve it: in the benchmark `local_search` is now >= `global_greedy`
   everywhere and strictly better where one swap escapes the greedy's optimum (it reaches a perfect
   zero-wait schedule on the asymmetric scenario, and 519->455 on the worst case). `selfish_greedy`
   stays as a deliberately naive baseline that shows how much good scoring closes the gap. This makes
   the factory default `local_search` genuinely the best option, not just a plausible one.

PERF FOOTNOTE: the "~29 s" a 20-trip run once took was almost entirely DEBUG logging + console
rendering, NOT the algorithm. At INFO/WARNING the heaviest scenario schedules in well under a second,
so the CLI and the UI run at INFO and need no result caching.

### Decision 23 - The UI is a thin presentation layer in its own package
`app/ui/streamlit_app.py` is the web app, run with `uv run streamlit run app/ui/streamlit_app.py`
(Decision 3B keeps it a SEPARATE process from the CLI). It is pure presentation: it loads scenarios
through `ScenarioLoader`, schedules the picked one through `create_scheduler()` (the factory default),
and renders the `ScheduleResult` -- it contains NO scheduling/simulation/scoring logic, so the exact
same engine powers the CLI and the UI.

KEY CHOICES:
- ONE dropdown, per the spec: pick a SCENARIO. There is deliberately no strategy picker and no
  metrics dashboard (Decision 20). The three views match the brief: scenario INPUT (route, stations,
  weights, trips), PER-BUS timetable (charges, waits, final arrival), PER-STATION order.
- SCHEDULE LIVE, don't cache the result. Scheduling is <1s, so every selection re-runs the real
  engine; only the static scenario FILES are cached (`st.cache_resource`). This keeps the UI honestly
  reactive and proves the engine is fast.
- TIMES shown as real `HH:MM` wall-clock. The domain stores only relative minutes, but the loader
  now keeps the `reference_time` anchor on the `Scenario` as `reference_minutes` (Decision 15
  refined), so the UI adds the two and renders the actual departure/arrival clock a dispatcher would
  read (19:00, 21:15, ...) instead of bare elapsed minutes.
- sys.path BOOTSTRAP: `streamlit run` puts the script's own dir on `sys.path`, not the project root,
  so the file inserts the project root before importing `app` (the `# noqa: E402` imports document
  this). A reviewer can therefore launch it from the project root with no packaging step.
- DEPLOYMENT: `requirements.txt` lists the three runtime deps (streamlit, pydantic, pyyaml) for
  Streamlit Community Cloud; `ortools` is intentionally excluded there (only the offline benchmark
  oracle uses it) to keep the cloud build lean.

## Open assumptions to defend
- The UI is a thin Streamlit presentation layer (`app/ui/`), run as a separate process; one scenario
  dropdown, three read-only views, live (uncached) scheduling, times shown as real HH:MM clock from
  the scenario's `reference_minutes` anchor (Decision 23).
- A scenario is one self-describing YAML (route+stops+weights+fleet_defaults+trips); the loader owns
  the format and the HH:MM->minute conversion; route is embedded per file on purpose (Decision 21).
- Scheduler is chosen by name via a factory (default `local_search`); strategies are internal, the
  UI exposes only the scenario dropdown per spec, no strategy picker (Decision 20).
- Local search seeds from the GLOBAL greedy and refines it by single-trip plan swaps re-scored by the
  global cost; it is never worse than the seed and finds a LOCAL optimum, not guaranteed global
  (benchmark-driven, Decision 22; mechanism in Decision 19).
- The rule registry self-populates by importing `builtin` in `app/rules/__init__.py`; every registry
  consumer sees the built-in rules without a manual import (Decision 22).
- Two greedy seeds exist: global-cost and selfish-arrival; selfish's incremental charger estimate is
  approximate (commit-order, not arrival-order) but only affects plan CHOICE, never the final
  exact simulation (Decision 18).
- Greedy order = arrival time at each contended station; ties broken by bus id (Decision 13d).
- Same-arrival lane tie-break is a simulator/heap-key concern, NOT cost; pluggable later (16).
- Charge always to full even when unnecessary (per spec).
- Charger conflict resolved at request time (greedy), not globally (until local search).
- Greedy scores each commit by GLOBAL weighted cost, not the bus's own arrival; scales for this
  size, would need incremental scoring / pruning / CP-SAT for large fleets (Decision 17).
- Direction is generic FORWARD/REVERSE; route endpoints carry the actual city names (13a).
- range_km/speed_kmph are Bus properties; charge_minutes is a Station property (13c).
- position_km lives on RouteStop (route-relative), not on Station identity (Decision 14).
- Time is relative minutes from 0 now; real-time anchor deferred to the loader (Decision 15).
