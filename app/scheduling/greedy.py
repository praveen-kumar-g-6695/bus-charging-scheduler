"""Two greedy schedulers that differ ONLY in how they score a candidate plan.

Both commit one plan per trip in departure order, in a single pass, never
revisiting an earlier trip. The only difference is the LENS they judge a candidate
through -- and that one difference is the whole point of having both:

* ``GlobalGreedyScheduler`` ("global" lens, design 1A): scores a candidate by
  RE-SIMULATING the whole schedule-so-far (committed trips + this candidate) and
  taking the SAME global weighted cost the final answer is judged by. Every weight
  steers every decision, but each candidate costs a full re-simulation, so the
  pass is roughly quadratic in the number of trips.

* ``SelfishGreedyScheduler`` ("selfish" lens, like ``main.py``): scores a
  candidate by THIS trip's OWN predicted arrival only, read cheaply off an
  incrementally-maintained set of chargers (no whole-schedule re-simulation). It
  is roughly linear in trips and far faster at scale, but blind to operator
  fairness and overall wait, so its seed quality is lower.

Both are SEEDS for the later local search; we keep both so their quality and speed
can be benchmarked against each other (see ARCHITECTURE Decision 18). They share
the commit-loop skeleton here and fill in only the per-candidate scoring.
"""

from abc import abstractmethod

from app.domain.models import Scenario, Trip
from app.logging_config import get_logger
from app.objective.cost import CostFunction
from app.plans.generator import ChargingPlan, PlanGenerator
from app.scheduling.base import SchedulerStrategy
from app.simulation.charger import ChargerStation
from app.simulation.simulator import ScheduleSimulator

logger = get_logger(__name__)


class _GreedyBase(SchedulerStrategy):
    """Shared single-pass commit loop for the greedy schedulers.

    Holds the parts both variants share -- deterministic trip order and the
    commit-one-trip-at-a-time loop -- and delegates the single differing step,
    "which candidate is best for this trip", to ``_pick_plan``. Subclasses may also
    keep per-pass state (the selfish one tracks chargers) via ``_begin`` and
    ``_after_commit``.
    """

    def _choose_plans(
        self,
        scenario: Scenario,
        generator: PlanGenerator,
        simulator: ScheduleSimulator,
        cost_function: CostFunction,
    ) -> dict[Trip, ChargingPlan]:
        """Commit one plan per trip in departure order, delegating the choice.

        Args:
            scenario: The scenario being scheduled.
            generator: Produces each trip's range-valid feasible plans.
            simulator: The shared simulator (used by the global variant).
            cost_function: Scores a simulated schedule; lower is better.

        Returns:
            One chosen ``ChargingPlan`` for every trip in the scenario.
        """
        ordered_trips = self._trips_in_commit_order(scenario)
        pass_state = self._begin(scenario)

        committed_plans: dict[Trip, ChargingPlan] = {}
        committed_trips: list[Trip] = []

        for trip in ordered_trips:
            feasible = generator.feasible_plans(trip)
            logger.debug(
                f"choosing plan for {trip.bus.bus_id} ({trip.direction.value}): "
                f"{len(feasible)} candidate(s)"
            )

            chosen_plan = self._pick_plan(
                pass_state,
                scenario,
                trip,
                feasible,
                committed_trips,
                committed_plans,
                cost_function,
            )

            committed_plans[trip] = chosen_plan
            committed_trips.append(trip)
            self._after_commit(pass_state, scenario, trip, chosen_plan)
            logger.info(f"committed {trip.bus.bus_id}: charge at {chosen_plan.station_names}")

        return committed_plans

    def _trips_in_commit_order(self, scenario: Scenario) -> list[Trip]:
        """Return the scenario's trips ordered for greedy commitment.

        Earliest departure first, then bus id, so the pass is deterministic.

        Args:
            scenario: The scenario whose trips are being ordered.

        Returns:
            The trips sorted by ``(departure_minute, bus_id)``.
        """
        trips = list(scenario.trips)
        trips.sort(key=lambda one_trip: (one_trip.departure_minute, one_trip.bus.bus_id))
        return trips

    def _begin(self, scenario: Scenario) -> object:
        """Create any per-pass state a variant needs (default: none).

        Args:
            scenario: The scenario being scheduled.

        Returns:
            Opaque state handed back to ``_pick_plan`` and ``_after_commit``;
            ``None`` for variants that keep no state.
        """
        return None

    def _after_commit(
        self,
        pass_state: object,
        scenario: Scenario,
        trip: Trip,
        chosen_plan: ChargingPlan,
    ) -> None:
        """React to a trip being committed (default: do nothing).

        Args:
            pass_state: The state returned by ``_begin``.
            scenario: The scenario being scheduled.
            trip: The trip just committed.
            chosen_plan: The plan committed for it.
        """

    @abstractmethod
    def _pick_plan(
        self,
        pass_state: object,
        scenario: Scenario,
        trip: Trip,
        feasible: list[ChargingPlan],
        committed_trips: list[Trip],
        committed_plans: dict[Trip, ChargingPlan],
        cost_function: CostFunction,
    ) -> ChargingPlan:
        """Return the best feasible plan for this trip under the variant's lens.

        Args:
            pass_state: The state returned by ``_begin``.
            scenario: The scenario being scheduled.
            trip: The trip we are choosing a plan for.
            feasible: This trip's range-valid candidate plans.
            committed_trips: The trips already locked in, in commit order.
            committed_plans: The plan chosen for each committed trip.
            cost_function: Scores a simulated schedule; lower is better.

        Returns:
            The chosen candidate plan.
        """


class GlobalGreedyScheduler(_GreedyBase):
    """Greedy that scores each candidate by the global weighted cost (design 1A).

    For each candidate it re-simulates the committed trips plus this trip and
    scores that partial schedule with the scenario's cost function, committing the
    cheapest. This makes every weight (individual, operator, overall) influence the
    choice, at the price of a full re-simulation per candidate.
    """

    def _pick_plan(
        self,
        pass_state: object,
        scenario: Scenario,
        trip: Trip,
        feasible: list[ChargingPlan],
        committed_trips: list[Trip],
        committed_plans: dict[Trip, ChargingPlan],
        cost_function: CostFunction,
    ) -> ChargingPlan:
        """Return the candidate giving the lowest global cost for the schedule-so-far.

        Args:
            pass_state: Unused; this variant keeps no per-pass state.
            scenario: The scenario being scheduled (for route and weights).
            trip: The trip we are choosing a plan for.
            feasible: This trip's range-valid candidate plans.
            committed_trips: The trips already locked in, in commit order.
            committed_plans: The plan chosen for each committed trip.
            cost_function: Scores a simulated schedule; lower is better.

        Returns:
            The candidate plan with the lowest resulting global cost.
        """
        best_plan: ChargingPlan | None = None
        best_cost: float | None = None

        for candidate in feasible:
            trial_trips = [*committed_trips, trip]
            trial_plans = dict(committed_plans)
            trial_plans[trip] = candidate

            partial_scenario = Scenario(
                name=f"{scenario.name}:partial",
                route=scenario.route,
                weights=scenario.weights,
                trips=tuple(trial_trips),
            )
            trial_result = ScheduleSimulator(partial_scenario).simulate(trial_plans)
            trial_cost = cost_function.score(trial_result)

            logger.debug(f"  candidate {candidate.station_names}: global cost {trial_cost}")

            if best_cost is None or trial_cost < best_cost:
                best_cost = trial_cost
                best_plan = candidate
                logger.debug(
                    f"    new best for {trip.bus.bus_id}: "
                    f"{candidate.station_names} @ cost {trial_cost}"
                )

        return best_plan


class SelfishGreedyScheduler(_GreedyBase):
    """Greedy that scores each candidate by the trip's OWN arrival (like main.py).

    Keeps one ``ChargerStation`` per station, advanced incrementally as trips
    commit. A candidate is scored by walking this trip through those chargers with
    the read-only ``peek`` (no reservation) and taking its predicted final arrival;
    the earliest-arriving candidate wins. After committing, the chosen plan's
    stations are actually reserved with ``request``. No whole-schedule re-simulation
    happens, so the pass is far cheaper -- but operator fairness and overall wait
    are invisible to the choice.

    NOTE: the final ``ScheduleResult`` is still produced by the real arrival-order
    simulator in the base ``schedule`` template, so this approximate charger
    bookkeeping only affects which plans are CHOSEN, not the reported schedule.
    """

    def _begin(self, scenario: Scenario) -> dict[str, ChargerStation]:
        """Create a fresh charger per station for incremental occupancy tracking.

        Args:
            scenario: The scenario being scheduled.

        Returns:
            A mapping from station name to a fresh ``ChargerStation``.
        """
        chargers_by_name: dict[str, ChargerStation] = {}
        for stop in scenario.route.stops:
            chargers_by_name[stop.station.name] = ChargerStation(stop.station)
        return chargers_by_name

    def _pick_plan(
        self,
        pass_state: object,
        scenario: Scenario,
        trip: Trip,
        feasible: list[ChargingPlan],
        committed_trips: list[Trip],
        committed_plans: dict[Trip, ChargingPlan],
        cost_function: CostFunction,
    ) -> ChargingPlan:
        """Return the candidate with the earliest predicted arrival for this trip.

        Args:
            pass_state: The chargers-by-name map from ``_begin``.
            scenario: The scenario being scheduled.
            trip: The trip we are choosing a plan for.
            feasible: This trip's range-valid candidate plans.
            committed_trips: Unused; occupancy is tracked in ``pass_state``.
            committed_plans: Unused; occupancy is tracked in ``pass_state``.
            cost_function: Unused; the selfish lens ignores the global cost.

        Returns:
            The candidate plan that gets this trip to its destination soonest.
        """
        chargers_by_name: dict[str, ChargerStation] = pass_state
        distance_by_name = self._distance_by_name(scenario, trip)

        best_plan: ChargingPlan | None = None
        best_arrival: int | None = None

        for candidate in feasible:
            predicted_arrival = self._walk_arrival(
                scenario, trip, candidate, distance_by_name, chargers_by_name, reserve=False
            )
            logger.debug(f"  candidate {candidate.station_names}: own arrival {predicted_arrival}")

            if best_arrival is None or predicted_arrival < best_arrival:
                best_arrival = predicted_arrival
                best_plan = candidate
                logger.debug(
                    f"    new best for {trip.bus.bus_id}: "
                    f"{candidate.station_names} @ arrival {predicted_arrival}"
                )

        return best_plan

    def _after_commit(
        self,
        pass_state: object,
        scenario: Scenario,
        trip: Trip,
        chosen_plan: ChargingPlan,
    ) -> None:
        """Reserve the chosen plan's chargers so later trips see this trip's load.

        Args:
            pass_state: The chargers-by-name map from ``_begin``.
            scenario: The scenario being scheduled.
            trip: The trip just committed.
            chosen_plan: The plan committed for it.
        """
        chargers_by_name: dict[str, ChargerStation] = pass_state
        distance_by_name = self._distance_by_name(scenario, trip)
        self._walk_arrival(
            scenario, trip, chosen_plan, distance_by_name, chargers_by_name, reserve=True
        )

    def _distance_by_name(self, scenario: Scenario, trip: Trip) -> dict[str, int]:
        """Map each station's name to its distance-from-origin for this trip.

        Args:
            scenario: The scenario being scheduled.
            trip: The trip whose direction fixes the distances.

        Returns:
            A mapping from station name to distance-from-origin in km.
        """
        distance_by_name: dict[str, int] = {}
        for station, distance in scenario.route.station_sequence(trip.direction):
            distance_by_name[station.name] = distance
        return distance_by_name

    def _walk_arrival(
        self,
        scenario: Scenario,
        trip: Trip,
        plan: ChargingPlan,
        distance_by_name: dict[str, int],
        chargers_by_name: dict[str, ChargerStation],
        reserve: bool,
    ) -> int:
        """Walk a trip through a plan and return its predicted final arrival.

        With ``reserve`` false the chargers are only peeked (state untouched), which
        is the scoring path; with it true each station is actually reserved, which
        is the commit path. Both share this one walk so prediction and commitment
        can never diverge.

        Args:
            scenario: The scenario being scheduled (for route length).
            trip: The trip being walked.
            plan: The candidate plan whose stations the trip charges at.
            distance_by_name: Station-name to distance-from-origin for this trip.
            chargers_by_name: The incrementally-tracked chargers.
            reserve: When true, reserve each lane; when false, only peek.

        Returns:
            The trip's predicted arrival minute at the destination.
        """
        bus = trip.bus
        current_time = trip.departure_minute
        current_position = 0

        for station in plan.stations:
            station_distance = distance_by_name[station.name]
            leg_km = station_distance - current_position
            arrival_minute = current_time + bus.travel_minutes_for_km(leg_km)

            charger = chargers_by_name[station.name]
            if reserve:
                start_minute = charger.request(arrival_minute)
            else:
                start_minute = charger.peek(arrival_minute)

            current_time = start_minute + station.charge_minutes
            current_position = station_distance

        final_leg_km = scenario.route.total_length_km - current_position
        return current_time + bus.travel_minutes_for_km(final_leg_km)
