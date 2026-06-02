"""Local search: take a greedy SEED schedule and refine it by swapping plans.

A greedy pass commits each trip's plan once and never looks back, so it can get
stuck just short of a better arrangement (e.g. two trips that would each be happier
if they SWAPPED stations, but neither swap looked best at its own commit moment).
Local search fixes exactly that: start from the greedy seed and repeatedly try
changing ONE trip's plan at a time, re-simulating the WHOLE schedule and re-scoring
it with the SAME global cost function. Any single change that lowers the global cost
is kept; we keep going until no single change helps -- a "local optimum".

This is the classic hill-climb / steepest-descent shape:

    seed -> look at all one-trip-swap neighbours -> take the best improving one
         -> repeat from there -> stop when no neighbour improves.

It reuses the trusted simulator and cost function unchanged, so it always optimises
the real objective (every weight still steers it). It is still a HEURISTIC -- it
finds a local, not necessarily global, optimum -- which is why the CP-SAT oracle in
the benchmark exists to bound how far off it can be.
"""

from app.domain.models import Scenario, Trip
from app.logging_config import get_logger
from app.objective.cost import CostFunction
from app.plans.generator import ChargingPlan, PlanGenerator
from app.scheduling.base import SchedulerStrategy
from app.scheduling.greedy import SelfishGreedyScheduler
from app.simulation.simulator import ScheduleSimulator

logger = get_logger(__name__)


class LocalSearchScheduler(SchedulerStrategy):
    """Refines a greedy seed by hill-climbing single-trip plan swaps.

    The seed scheduler (selfish greedy by default) provides a starting plan per
    trip; this strategy then improves it by steepest descent: each round it scores
    every "change one trip to a different feasible plan" neighbour, applies the
    single most-improving one, and repeats until no neighbour lowers the global
    cost or the iteration cap is hit. The seed is pluggable so we can later compare
    refining the selfish seed versus the global-greedy seed (ARCHITECTURE
    Decision 18).
    """

    def __init__(
        self,
        seed: SchedulerStrategy | None = None,
        max_iterations: int = 100,
    ) -> None:
        """Configure the seed strategy and the safety cap on refinement rounds.

        Args:
            seed: The strategy that produces the starting schedule. Defaults to
                ``SelfishGreedyScheduler`` (cheap seed; swap to the global greedy
                later if a higher-quality start is wanted).
            max_iterations: Hard cap on improvement rounds, so the search always
                terminates even if (in principle) it kept finding tiny gains.
        """
        self._seed = seed if seed is not None else SelfishGreedyScheduler()
        self._max_iterations = max_iterations

    def _choose_plans(
        self,
        scenario: Scenario,
        generator: PlanGenerator,
        simulator: ScheduleSimulator,
        cost_function: CostFunction,
    ) -> dict[Trip, ChargingPlan]:
        """Seed from the greedy, then hill-climb single-trip swaps to a local optimum.

        Args:
            scenario: The scenario being scheduled.
            generator: Produces each trip's range-valid feasible plans.
            simulator: The shared simulator used to score every neighbour.
            cost_function: Scores a simulated schedule; lower is better.

        Returns:
            The refined chosen plan for every trip in the scenario.
        """
        current_plans = self._seed_plans(scenario, generator, simulator, cost_function)
        current_cost = self._cost_of(current_plans, simulator, cost_function)
        logger.info(
            f"local search starting from {self._seed.__class__.__name__} seed: cost {current_cost}"
        )

        feasible_by_trip = self._feasible_by_trip(scenario, generator)

        for iteration in range(1, self._max_iterations + 1):
            best_move = self._best_improving_move(
                scenario, current_plans, current_cost, feasible_by_trip, simulator, cost_function
            )
            if best_move is None:
                logger.info(
                    f"local search reached a local optimum after {iteration - 1} "
                    f"improving move(s): cost {current_cost}"
                )
                break

            improved_trip, improved_plan, improved_cost = best_move
            logger.info(
                f"iteration {iteration}: swap {improved_trip.bus.bus_id} -> "
                f"{improved_plan.station_names} lowers cost {current_cost} -> {improved_cost}"
            )
            current_plans[improved_trip] = improved_plan
            current_cost = improved_cost
        else:
            logger.warning(
                f"local search hit the iteration cap ({self._max_iterations}); "
                f"stopping at cost {current_cost}"
            )

        return current_plans

    def _seed_plans(
        self,
        scenario: Scenario,
        generator: PlanGenerator,
        simulator: ScheduleSimulator,
        cost_function: CostFunction,
    ) -> dict[Trip, ChargingPlan]:
        """Ask the seed strategy for its chosen plan per trip (the starting point).

        Reuses the seed's own decision step with the shared tools, so the seed
        behaves exactly as it would on its own -- local search only takes over
        afterwards.

        Args:
            scenario: The scenario being scheduled.
            generator: Produces each trip's range-valid feasible plans.
            simulator: The shared simulator.
            cost_function: Scores a simulated schedule; lower is better.

        Returns:
            The seed strategy's chosen plan for every trip.
        """
        return self._seed._choose_plans(scenario, generator, simulator, cost_function)

    def _feasible_by_trip(
        self,
        scenario: Scenario,
        generator: PlanGenerator,
    ) -> dict[Trip, list[ChargingPlan]]:
        """Pre-compute each trip's feasible plans once, to reuse every round.

        Args:
            scenario: The scenario being scheduled.
            generator: Produces each trip's range-valid feasible plans.

        Returns:
            A mapping from trip to its list of feasible candidate plans.
        """
        feasible_by_trip: dict[Trip, list[ChargingPlan]] = {}
        for trip in scenario.trips:
            feasible_by_trip[trip] = generator.feasible_plans(trip)
        return feasible_by_trip

    def _best_improving_move(
        self,
        scenario: Scenario,
        current_plans: dict[Trip, ChargingPlan],
        current_cost: float,
        feasible_by_trip: dict[Trip, list[ChargingPlan]],
        simulator: ScheduleSimulator,
        cost_function: CostFunction,
    ) -> tuple[Trip, ChargingPlan, float] | None:
        """Find the single trip-swap that lowers the global cost the most.

        Considers every "give trip T a different feasible plan" neighbour, scores
        the whole resulting schedule, and returns the strictly-best improvement.
        Ties keep the first-found (trips iterate in scenario order) for determinism.

        Args:
            scenario: The scenario being scheduled.
            current_plans: The current plan per trip (not mutated here).
            current_cost: The global cost of ``current_plans``.
            feasible_by_trip: Each trip's pre-computed feasible plans.
            simulator: The shared simulator used to score neighbours.
            cost_function: Scores a simulated schedule; lower is better.

        Returns:
            A ``(trip, plan, new_cost)`` triple for the best improving swap, or
            ``None`` if no single swap lowers the cost.
        """
        best_trip: Trip | None = None
        best_plan: ChargingPlan | None = None
        best_cost = current_cost

        for trip in scenario.trips:
            present_plan = current_plans[trip]
            for candidate in feasible_by_trip[trip]:
                if candidate == present_plan:
                    continue

                trial_plans = dict(current_plans)
                trial_plans[trip] = candidate
                trial_cost = self._cost_of(trial_plans, simulator, cost_function)
                logger.debug(
                    f"  neighbour {trip.bus.bus_id} -> {candidate.station_names}: "
                    f"cost {trial_cost} (current {current_cost})"
                )

                if trial_cost < best_cost:
                    best_cost = trial_cost
                    best_trip = trip
                    best_plan = candidate

        if best_trip is None or best_plan is None:
            return None
        return best_trip, best_plan, best_cost

    def _cost_of(
        self,
        plan_by_trip: dict[Trip, ChargingPlan],
        simulator: ScheduleSimulator,
        cost_function: CostFunction,
    ) -> float:
        """Simulate a full set of plans and return its global weighted cost.

        Args:
            plan_by_trip: The plan chosen for every trip.
            simulator: The shared simulator.
            cost_function: Scores a simulated schedule; lower is better.

        Returns:
            The global weighted cost of simulating ``plan_by_trip``.
        """
        result = simulator.simulate(plan_by_trip)
        return cost_function.score(result)
