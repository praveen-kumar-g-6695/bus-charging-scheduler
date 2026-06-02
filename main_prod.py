"""main_prod.py -- the production entry point.

This is deliberately THIN. Its only jobs are:
  1. Initialise logging ONCE, before anything else happens.
  2. Build (or, later, load) a scenario.
  3. Hand it to the scheduler and show the result.

Run it with:
    uv run python main_prod.py

Right now (STEP 2) there is still no scheduler -- we stand up the logging
infrastructure, the INPUT domain models, and the OUTPUT result models, and log
them at DEBUG so you can see the foundation working end to end.
"""

from app.logging_config import get_logger, setup_logging


def build_demo_scenario():
    """Construct a tiny hand-made scenario to exercise the STEP 1 models.

    The domain imports are done lazily inside the function (after logging is set
    up) so that any debug logs emitted while building the models actually appear.

    Returns:
        A :class:`app.domain.models.Scenario` with one trip in each direction.
    """
    from app.domain.enums import Direction, Operator
    from app.domain.models import Bus, Route, RouteStop, Scenario, Station, Trip, Weights

    logger = get_logger(__name__)
    logger.info("building a small demo scenario to test the STEP 1 models")

    # The real Bengaluru -> Kochi route: A,B,C,D at km 100, 220, 320, 440. Each
    # station is a position-free physical charger; the RouteStop places it on this
    # route at a given km (Decision 14).
    route = Route(
        name="Bengaluru-Kochi",
        origin="Bengaluru",
        destination="Kochi",
        total_length_km=540,
        stops=(
            RouteStop(station=Station(name="A", chargers=1, charge_minutes=25), position_km=100),
            RouteStop(station=Station(name="B", chargers=1, charge_minutes=25), position_km=220),
            RouteStop(station=Station(name="C", chargers=1, charge_minutes=25), position_km=320),
            RouteStop(station=Station(name="D", chargers=1, charge_minutes=25), position_km=440),
        ),
    )
    logger.debug(
        f"route built: {route.name} ({route.origin} -> {route.destination}), "
        f"total {route.total_length_km} km, {len(route.stops)} stops"
    )

    # Two physical buses (vehicle physics only -- no direction/departure here).
    bus_one = Bus(bus_id="bus-01", operator=Operator.KPN, range_km=240, speed_kmph=60)
    bus_two = Bus(bus_id="bus-02", operator=Operator.FRESHBUS, range_km=240, speed_kmph=60)

    # Two trips, one in each direction, to prove the bidirectional model. The
    # direction and departure live on the TRIP, not on the bus.
    trips = (
        Trip(bus=bus_one, direction=Direction.FORWARD, departure_minute=0),
        Trip(bus=bus_two, direction=Direction.REVERSE, departure_minute=0),
    )

    scenario = Scenario(
        name="demo",
        route=route,
        weights=Weights(),  # defaults: individual=1, operator=1, overall=1
        trips=trips,
    )
    logger.info(f"scenario '{scenario.name}' built with {len(scenario.trips)} trips")
    return scenario


def build_demo_result(scenario):
    """Hand-build one ScheduleResult to exercise the STEP 2 result models.

    There is no scheduler yet (that arrives in a later step), so we fabricate a
    small but physically-consistent outcome by hand: each trip makes one charge,
    and we deliberately make the second trip WAIT so the wait metrics are not all
    zero. The point is only to show the output data shapes working.

    Args:
        scenario: The scenario whose trips we are inventing an outcome for.

    Returns:
        A :class:`app.domain.results.ScheduleResult` for the demo scenario.
    """
    from app.domain.results import ChargeEvent, ScheduleResult, TripTimeline

    logger = get_logger(__name__)
    logger.info("hand-building a demo ScheduleResult to test the STEP 2 models")

    # Both demo trips charge at the route's first stop. Trip one arrives first and
    # charges 100..125; trip two arrives at 100 too but must WAIT until 125, then
    # charges 125..150 -- a 25-minute wait, on purpose, so the metrics are
    # interesting. We reference the REAL station object, not its name.
    first_trip = scenario.trips[0]
    second_trip = scenario.trips[1]
    station_a = scenario.route.stops[0].station

    first_event = ChargeEvent(
        trip=first_trip,
        station=station_a,
        arrival_minute=100,
        start_minute=100,
    )
    second_event = ChargeEvent(
        trip=second_trip,
        station=station_a,
        arrival_minute=100,
        start_minute=125,
    )
    logger.debug(
        f"{second_event.trip.bus.bus_id} waited {second_event.wait_minutes} min at "
        f"{second_event.station.name} (charger busy until {first_event.end_minute})"
    )

    first_timeline = TripTimeline(
        trip=first_trip,
        charge_events=(first_event,),
        arrival_minute=540,
    )
    second_timeline = TripTimeline(
        trip=second_trip,
        charge_events=(second_event,),
        arrival_minute=565,
    )

    result = ScheduleResult(
        scenario=scenario,
        trip_timelines=(first_timeline, second_timeline),
    )
    logger.info(f"result built for scenario '{result.scenario.name}'")
    return result


def demonstrate_plans(scenario) -> None:
    """Log the range-valid charging plans for every trip in the scenario.

    There is no scheduler yet; this only shows the PlanGenerator producing each
    trip's legal menu of charging options from geometry and range alone.

    Args:
        scenario: The scenario whose trips we enumerate plans for.

    Returns:
        None.
    """
    from app.plans.generator import PlanGenerator

    logger = get_logger(__name__)
    logger.info("--- feasible charging plans per trip (geometry + range) ---")

    generator = PlanGenerator(scenario.route)
    for trip in scenario.trips:
        plans = generator.feasible_plans(trip)
        logger.info(
            f"{trip.bus.bus_id} ({trip.direction.value}, range "
            f"{trip.bus.range_km} km): {len(plans)} feasible plan(s)"
        )
        for plan in plans:
            shown = plan.station_names if plan.station_names else "(no charging)"
            logger.info(f"    {shown}")

    logger.info("=== STEP 3 complete: plan generation is working ===")


def demonstrate_simulation(scenario) -> None:
    """Simulate a chosen plan per trip and log the concrete schedule.

    To make charger contention visible we build two FORWARD trips that both
    charge at station A with departures only ten minutes apart on a single-lane
    charger, so the second bus must queue. There is still no scheduler choosing
    plans -- we hand-pick one feasible plan per trip just to drive the simulator.

    Args:
        scenario: The scenario whose route we reuse for the demo trips.

    Returns:
        None.
    """
    from app.domain.enums import Direction, Operator
    from app.domain.models import Bus, Scenario, Trip
    from app.plans.generator import PlanGenerator
    from app.simulation.simulator import ScheduleSimulator

    logger = get_logger(__name__)
    logger.info("--- simulating two forward trips contending for station A ---")

    bus_one = Bus(bus_id="bus-A1", operator=Operator.KPN, range_km=240, speed_kmph=60)
    bus_two = Bus(bus_id="bus-A2", operator=Operator.FRESHBUS, range_km=240, speed_kmph=60)
    trip_one = Trip(bus=bus_one, direction=Direction.FORWARD, departure_minute=0)
    trip_two = Trip(bus=bus_two, direction=Direction.FORWARD, departure_minute=10)

    contention_scenario = Scenario(
        name="contention-demo",
        route=scenario.route,
        weights=scenario.weights,
        trips=(trip_one, trip_two),
    )

    # Hand both trips the same feasible plan that charges at A first (e.g. A, C),
    # so they collide on station A's single lane.
    generator = PlanGenerator(contention_scenario.route)
    plan_by_trip = {}
    for trip in contention_scenario.trips:
        for plan in generator.feasible_plans(trip):
            if plan.station_names and plan.station_names[0] == "A":
                plan_by_trip[trip] = plan
                break

    simulator = ScheduleSimulator(contention_scenario)
    result = simulator.simulate(plan_by_trip)

    logger.info("--- resulting schedule (per trip) ---")
    for timeline in result.trip_timelines:
        bus = timeline.trip.bus
        logger.info(
            f"{bus.bus_id} ({bus.operator.value}): "
            f"{len(timeline.charge_events)} charge(s), "
            f"waited {timeline.total_wait_minutes} min, arrived min {timeline.arrival_minute}"
        )
        for event in timeline.charge_events:
            logger.info(
                f"    {event.station.name}: arrive {event.arrival_minute}, "
                f"charge {event.start_minute}..{event.end_minute} "
                f"(waited {event.wait_minutes} min)"
            )

    logger.info(f"total wait across both trips = {result.total_wait_minutes()} min")
    logger.info("=== STEP 4 complete: the simulator is working ===")


def demonstrate_rules(scenario) -> None:
    """Score a simulated schedule with each registered rule and log the breakdown.

    Reuses the same contended two-forward-trip schedule from Step 4, then asks the
    rule registry for every known rule and shows each rule's raw (unweighted) cost
    alongside the scenario's weight for it. There is still no cost function summing
    them -- that is Step 6; here we just prove the registry and rules work.

    Args:
        scenario: The scenario whose route and weights drive the demo.

    Returns:
        None.
    """
    from app.domain.enums import Direction, Operator
    from app.domain.models import Bus, Scenario, Trip
    from app.plans.generator import PlanGenerator

    # Importing builtin is what populates the registry via @register_rule.
    from app.rules import builtin as _builtin_rules  # noqa: F401
    from app.rules.base import RuleContext
    from app.rules.registry import get_registry
    from app.simulation.simulator import ScheduleSimulator

    logger = get_logger(__name__)
    logger.info("--- scoring a schedule with every registered rule ---")

    bus_one = Bus(bus_id="bus-R1", operator=Operator.KPN, range_km=240, speed_kmph=60)
    bus_two = Bus(bus_id="bus-R2", operator=Operator.FRESHBUS, range_km=240, speed_kmph=60)
    trip_one = Trip(bus=bus_one, direction=Direction.FORWARD, departure_minute=0)
    trip_two = Trip(bus=bus_two, direction=Direction.FORWARD, departure_minute=10)

    rules_scenario = Scenario(
        name="rules-demo",
        route=scenario.route,
        weights=scenario.weights,
        trips=(trip_one, trip_two),
    )

    generator = PlanGenerator(rules_scenario.route)
    plan_by_trip = {}
    for trip in rules_scenario.trips:
        for plan in generator.feasible_plans(trip):
            if plan.station_names and plan.station_names[0] == "A":
                plan_by_trip[trip] = plan
                break

    result = ScheduleSimulator(rules_scenario).simulate(plan_by_trip)
    context = RuleContext(result=result)

    registry = get_registry()
    for rule in registry.create_all():
        raw_cost = rule.cost(context)
        weight = rules_scenario.weights.for_rule(rule.key)
        logger.info(
            f"rule '{rule.key}': raw cost {raw_cost} min, weight {weight} "
            f"-> weighted {raw_cost * weight}"
        )

    logger.info("=== STEP 5 complete: the rule registry is working ===")


def demonstrate_cost(scenario) -> None:
    """Collapse a schedule's rule costs into one number, and show weights steering it.

    Scores the same contended schedule twice: once with default weights (all 1.0)
    and once with the operator weight bumped to 2.0 (as scenario 4 does), so the
    single total visibly shifts when a weight changes -- which is exactly the lever
    the greedy scheduler will use in Step 7.

    Args:
        scenario: The scenario whose route drives the demo trips.

    Returns:
        None.
    """
    from app.domain.enums import Direction, Operator
    from app.domain.models import Bus, Scenario, Trip, Weights

    # Importing builtin is what populates the registry via @register_rule.
    from app.objective.cost import CostFunction
    from app.plans.generator import PlanGenerator
    from app.rules import builtin as _builtin_rules  # noqa: F401
    from app.simulation.simulator import ScheduleSimulator

    logger = get_logger(__name__)
    logger.info("--- collapsing rule costs into one weighted total ---")

    bus_one = Bus(bus_id="bus-C1", operator=Operator.KPN, range_km=240, speed_kmph=60)
    bus_two = Bus(bus_id="bus-C2", operator=Operator.FRESHBUS, range_km=240, speed_kmph=60)
    trip_one = Trip(bus=bus_one, direction=Direction.FORWARD, departure_minute=0)
    trip_two = Trip(bus=bus_two, direction=Direction.FORWARD, departure_minute=10)

    default_weights = Weights()
    cost_scenario = Scenario(
        name="cost-demo",
        route=scenario.route,
        weights=default_weights,
        trips=(trip_one, trip_two),
    )

    generator = PlanGenerator(cost_scenario.route)
    plan_by_trip = {}
    for trip in cost_scenario.trips:
        for plan in generator.feasible_plans(trip):
            if plan.station_names and plan.station_names[0] == "A":
                plan_by_trip[trip] = plan
                break

    result = ScheduleSimulator(cost_scenario).simulate(plan_by_trip)

    default_cost = CostFunction(default_weights).score(result)
    logger.info(f"total cost with default weights (all 1.0) = {default_cost}")

    operator_heavy = Weights(values={"individual": 1.0, "operator": 2.0, "overall": 1.0})
    heavy_cost = CostFunction(operator_heavy).score(result)
    logger.info(f"total cost with operator weight 2.0 = {heavy_cost}")

    logger.info("=== STEP 6 complete: the cost function is working ===")


def demonstrate_greedy(scenario) -> None:
    """Run both greedy schedulers on the same congested scenario and compare.

    Builds a small congested scenario (several forward trips leaving close
    together, so they compete for the single-lane stations) and lets BOTH greedy
    variants choose every trip's plan:

    * the GLOBAL greedy minimises the weighted global cost at each step, and
    * the SELFISH greedy minimises each trip's own arrival at each step.

    Running them side by side shows the quality difference the scoring lens makes;
    later we will benchmark their speed too (ARCHITECTURE Decision 18).

    Args:
        scenario: The scenario whose route and weights drive the demo.

    Returns:
        None.
    """
    from app.domain.enums import Direction, Operator
    from app.domain.models import Bus, Scenario, Trip

    # Importing builtin is what populates the registry via @register_rule.
    from app.rules import builtin as _builtin_rules  # noqa: F401
    from app.scheduling.greedy import GlobalGreedyScheduler, SelfishGreedyScheduler

    logger = get_logger(__name__)
    logger.info("--- greedy scheduling a small congested scenario ---")

    operators = (Operator.KPN, Operator.FRESHBUS, Operator.FLIXBUS)
    trips = []
    for trip_index in range(4):
        bus = Bus(
            bus_id=f"bus-G{trip_index + 1}",
            operator=operators[trip_index % len(operators)],
            range_km=240,
            speed_kmph=60,
        )
        trips.append(Trip(bus=bus, direction=Direction.FORWARD, departure_minute=trip_index * 5))

    greedy_scenario = Scenario(
        name="greedy-demo",
        route=scenario.route,
        weights=scenario.weights,
        trips=tuple(trips),
    )

    schedulers = (
        ("GLOBAL greedy (global cost lens)", GlobalGreedyScheduler()),
        ("SELFISH greedy (own-arrival lens)", SelfishGreedyScheduler()),
    )
    for label, scheduler in schedulers:
        logger.info(f"--- {label} ---")
        result = scheduler.schedule(greedy_scenario)
        for timeline in result.trip_timelines:
            bus = timeline.trip.bus
            charged_at = tuple(event.station.name for event in timeline.charge_events)
            logger.info(
                f"{bus.bus_id} ({bus.operator.value}): charge at {charged_at}, "
                f"waited {timeline.total_wait_minutes} min, arrived min {timeline.arrival_minute}"
            )
        logger.info(f"total wait across all trips = {result.total_wait_minutes()} min")

    logger.info("=== STEP 7 complete: both greedy schedulers are working ===")


def demonstrate_local_search(scenario) -> None:
    """Refine a greedy seed with local search and show the cost coming down.

    Uses a deliberately fairness-sensitive scenario: three buses leave together
    and the operator-fairness weight is turned up. The selfish greedy seed is
    BLIND to fairness (it only minimises each bus's own arrival), so it packs the
    schedule in a way that is unfair across operators. Local search, judging by the
    SAME global cost (where fairness now matters), then swaps one trip's plan to
    rebalance and drives the cost down -- exactly the gap a greedy seed leaves for
    refinement.

    Args:
        scenario: The scenario whose route drives the demo (weights overridden).

    Returns:
        None.
    """
    from app.domain.enums import Direction, Operator
    from app.domain.models import Bus, Scenario, Trip, Weights

    # Importing builtin is what populates the registry via @register_rule.
    from app.rules import builtin as _builtin_rules  # noqa: F401
    from app.scheduling.greedy import SelfishGreedyScheduler
    from app.scheduling.local_search import LocalSearchScheduler

    logger = get_logger(__name__)
    logger.info("--- local search refining a greedy seed ---")

    # Two buses from one operator, one from another: fairness is now in play.
    operators = (Operator.KPN, Operator.KPN, Operator.FRESHBUS)
    trips = []
    for trip_index in range(3):
        bus = Bus(
            bus_id=f"bus-L{trip_index + 1}",
            operator=operators[trip_index],
            range_km=240,
            speed_kmph=60,
        )
        trips.append(Trip(bus=bus, direction=Direction.FORWARD, departure_minute=0))

    # Turn the operator-fairness weight up so unfairness is expensive.
    fairness_weights = Weights(values={"individual": 1.0, "operator": 3.0, "overall": 1.0})
    search_scenario = Scenario(
        name="local-search-demo",
        route=scenario.route,
        weights=fairness_weights,
        trips=tuple(trips),
    )

    # Seed from the selfish greedy for now (pluggable: global greedy later).
    scheduler = LocalSearchScheduler(seed=SelfishGreedyScheduler())
    result = scheduler.schedule(search_scenario)

    logger.info("--- local search result (per trip) ---")
    for timeline in result.trip_timelines:
        bus = timeline.trip.bus
        charged_at = tuple(event.station.name for event in timeline.charge_events)
        logger.info(
            f"{bus.bus_id} ({bus.operator.value}): charge at {charged_at}, "
            f"waited {timeline.total_wait_minutes} min, arrived min {timeline.arrival_minute}"
        )

    logger.info(f"total wait across all trips = {result.total_wait_minutes()} min")
    logger.info("=== STEP 8 complete: local search is working ===")


def demonstrate_factory(scenario) -> None:
    """Build a scheduler purely by NAME via the factory and run it.

    Shows the single seam every caller (CLI, UI, tests) uses to get a scheduler:
    ``create_scheduler()`` returns the default (local search) with no class names
    in sight, and passing a different name swaps the whole algorithm in one line.
    This is the flexibility to point at during the live demo.

    Args:
        scenario: The scenario whose route and weights drive the demo.

    Returns:
        None.
    """
    from app.domain.enums import Direction, Operator
    from app.domain.models import Bus, Scenario, Trip

    # Importing builtin is what populates the registry via @register_rule.
    from app.rules import builtin as _builtin_rules  # noqa: F401
    from app.scheduling.factory import available_schedulers, create_scheduler

    logger = get_logger(__name__)
    logger.info("--- building a scheduler by name via the factory ---")
    logger.info(f"schedulers the factory knows: {available_schedulers()}")

    operators = (Operator.KPN, Operator.FRESHBUS, Operator.FLIXBUS)
    trips = []
    for trip_index in range(4):
        bus = Bus(
            bus_id=f"bus-F{trip_index + 1}",
            operator=operators[trip_index % len(operators)],
            range_km=240,
            speed_kmph=60,
        )
        trips.append(Trip(bus=bus, direction=Direction.FORWARD, departure_minute=trip_index * 5))

    factory_scenario = Scenario(
        name="factory-demo",
        route=scenario.route,
        weights=scenario.weights,
        trips=tuple(trips),
    )

    # No class name here -- just ask for the default (local search).
    scheduler = create_scheduler()
    logger.info(f"factory default built: {scheduler.__class__.__name__}")
    result = scheduler.schedule(factory_scenario)

    logger.info("--- factory-built scheduler result (per trip) ---")
    for timeline in result.trip_timelines:
        bus = timeline.trip.bus
        charged_at = tuple(event.station.name for event in timeline.charge_events)
        logger.info(
            f"{bus.bus_id} ({bus.operator.value}): charge at {charged_at}, "
            f"waited {timeline.total_wait_minutes} min, arrived min {timeline.arrival_minute}"
        )

    logger.info(f"total wait across all trips = {result.total_wait_minutes()} min")
    logger.info("=== STEP 9 complete: the scheduler factory is working ===")


def demonstrate_loader() -> None:
    """Load the shipped scenario files and schedule one end to end.

    Reads every ``scenarios/*.yaml`` via the loader (so we exercise the real data
    files reviewers will use), lists them as the UI dropdown will, then builds the
    default scheduler from the factory and runs it on the first scenario. This is
    the full production path -- data file in, scheduled result out -- with nothing
    hand-built.

    Returns:
        None.
    """
    from pathlib import Path

    from app.io.loader import ScenarioLoader

    # Importing builtin is what populates the registry via @register_rule.
    from app.rules import builtin as _builtin_rules  # noqa: F401
    from app.scheduling.factory import create_scheduler

    logger = get_logger(__name__)
    logger.info("--- loading scenario files from disk ---")

    scenarios_dir = Path(__file__).parent / "scenarios"
    scenarios = ScenarioLoader().load_dir(scenarios_dir)

    logger.info("--- scenarios available (as the UI dropdown will list them) ---")
    for scenario in scenarios:
        logger.info(
            f"'{scenario.name}': {len(scenario.trips)} trips, weights {scenario.weights.values}"
        )

    first_scenario = scenarios[0]
    logger.info(f"--- scheduling '{first_scenario.name}' with the factory default ---")
    scheduler = create_scheduler()
    result = scheduler.schedule(first_scenario)

    logger.info("--- result (per trip) ---")
    for timeline in result.trip_timelines:
        bus = timeline.trip.bus
        charged_at = tuple(event.station.name for event in timeline.charge_events)
        logger.info(
            f"{bus.bus_id} ({bus.operator.value}, {timeline.trip.direction.value}): "
            f"charge at {charged_at}, waited {timeline.total_wait_minutes} min, "
            f"arrived min {timeline.arrival_minute}"
        )

    logger.info(f"total wait across all trips = {result.total_wait_minutes()} min")
    logger.info("=== STEP 10 complete: scenario files load and schedule ===")


def main() -> None:
    """Run the STEP 1 + STEP 2 demo: domain models, then result models.

    Returns:
        None.
    """
    # STEP 1 of every run: turn logging on. Everything after this can log freely.
    setup_logging(level="INFO")
    logger = get_logger(__name__)
    logger.info("=== Bus Charging Scheduler (production) starting up ===")

    scenario = build_demo_scenario()

    # Show the tunable weights -- proof they live in one obvious place.
    logger.info(f"weights in use: {scenario.weights.values}")
    logger.debug(f"weight for 'operator' rule = {scenario.weights.for_rule('operator'):.1f}")
    logger.debug(
        f"weight for an UNKNOWN rule = "
        f"{scenario.weights.for_rule('does_not_exist_yet'):.1f} (defaults to 0.0)"
    )

    # Demonstrate the bidirectional core: the SAME route, seen from each end.
    logger.info("--- how each trip sees the route (station, km-from-its-origin) ---")
    for trip in scenario.trips:
        sequence = scenario.route.station_sequence(trip.direction)
        readable = []
        for station, distance_from_origin in sequence:
            readable.append(f"{station.name}@{distance_from_origin}km")
        joined = " -> ".join(readable)
        logger.info(
            f"{trip.bus.bus_id} ({trip.bus.operator.value}, {trip.direction.value}): {joined}"
        )

    # Show the km->minutes conversion the simulator will rely on later.
    first_bus = scenario.trips[0].bus
    logger.debug(
        f"driving 100 km takes {first_bus.travel_minutes_for_km(100)} minutes "
        f"at {first_bus.speed_kmph} km/h"
    )

    logger.info("=== STEP 1 complete: logging + domain models are working ===")

    # STEP 2: stand up the OUTPUT side -- the result models the simulator produces.
    result = build_demo_result(scenario)

    logger.info("--- per-trip view (TripTimeline) ---")
    for timeline in result.trip_timelines:
        bus = timeline.trip.bus
        logger.info(
            f"{bus.bus_id} ({bus.operator.value}, {timeline.trip.direction.value}): "
            f"{len(timeline.charge_events)} charge(s), "
            f"waited {timeline.total_wait_minutes} min, arrived min {timeline.arrival_minute}"
        )

    logger.info("--- per-station view (StationSchedule, derived) ---")
    for station_schedule in result.station_schedules():
        for event in station_schedule.events:
            logger.info(
                f"station {station_schedule.station.name}: {event.trip.bus.bus_id} "
                f"charged {event.start_minute}..{event.end_minute} "
                f"(waited {event.wait_minutes} min)"
            )

    logger.info("--- summary metrics (computed from the timelines) ---")
    logger.info(f"total wait across all trips = {result.total_wait_minutes()} min")
    logger.info(f"worst single-trip wait = {result.max_wait_minutes()} min")
    for operator, operator_wait in result.wait_by_operator().items():
        logger.info(f"operator {operator.value} total wait = {operator_wait} min")

    logger.info("=== STEP 2 complete: result models are working ===")

    # STEP 3: enumerate each trip's legal charging plans (geometry + range only).
    demonstrate_plans(scenario)

    # STEP 4: simulate hand-picked plans into a concrete schedule with contention.
    demonstrate_simulation(scenario)

    # STEP 5: score a simulated schedule with every registered rule.
    demonstrate_rules(scenario)

    # STEP 6: collapse the rule costs into one weighted total.
    demonstrate_cost(scenario)

    # STEP 7: let the greedy scheduler choose every trip's plan via global cost.
    demonstrate_greedy(scenario)

    # STEP 8: refine a greedy seed with local search (single-trip plan swaps).
    demonstrate_local_search(scenario)

    # STEP 9: build a scheduler by name via the factory (default = local search).
    demonstrate_factory(scenario)

    # STEP 10: load the shipped scenario files and schedule one end to end.
    demonstrate_loader()


if __name__ == "__main__":
    main()
