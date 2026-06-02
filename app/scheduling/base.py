"""The SchedulerStrategy base: a Template Method around plan-choosing.

A scheduler's job is always the same SHAPE: take a scenario, decide one charging
plan per trip, simulate that decision, and hand back the scored result. Only the
"decide" part differs between a greedy pass and a smarter search. So the skeleton
lives here once, and subclasses fill in just the decision step.
"""

from abc import ABC, abstractmethod

from app.domain.models import Scenario, Trip
from app.domain.results import ScheduleResult
from app.logging_config import get_logger
from app.objective.cost import CostFunction
from app.plans.generator import ChargingPlan, PlanGenerator
from app.simulation.simulator import ScheduleSimulator

logger = get_logger(__name__)


class SchedulerStrategy(ABC):
    """A pluggable algorithm for choosing every trip's charging plan.

    This is the Strategy interface AND a Template Method: ``schedule`` is the
    fixed skeleton (build the shared tools, delegate the decision, simulate, log
    the final cost), and ``_choose_plans`` is the single hole each concrete
    strategy fills. Subclasses never re-implement simulation or scoring, so every
    strategy is automatically consistent about what "a schedule" and "its cost"
    mean.
    """

    def schedule(self, scenario: Scenario) -> ScheduleResult:
        """Produce a scored schedule for the scenario (the fixed skeleton).

        Builds the tools every strategy shares -- a plan generator, a simulator and
        the scenario's cost function -- then delegates the actual choosing to the
        subclass, simulates the chosen plans, and returns the concrete result.

        Args:
            scenario: The scenario to schedule.

        Returns:
            The simulated ``ScheduleResult`` for the plans this strategy chose.
        """
        generator = PlanGenerator(scenario.route)
        simulator = ScheduleSimulator(scenario)
        cost_function = CostFunction(scenario.weights)

        logger.info(
            f"scheduling '{scenario.name}' with {self.__class__.__name__} "
            f"({len(scenario.trips)} trips)"
        )

        plan_by_trip = self._choose_plans(scenario, generator, simulator, cost_function)

        result = simulator.simulate(plan_by_trip)
        final_cost = cost_function.score(result)
        logger.info(f"{self.__class__.__name__} finished '{scenario.name}': cost {final_cost}")
        return result

    @abstractmethod
    def _choose_plans(
        self,
        scenario: Scenario,
        generator: PlanGenerator,
        simulator: ScheduleSimulator,
        cost_function: CostFunction,
    ) -> dict[Trip, ChargingPlan]:
        """Decide which feasible plan each trip should commit to.

        This is the one step that differs between strategies. Implementations are
        free to use the simulator and cost function as many times as they like to
        evaluate candidate decisions before settling on one plan per trip.

        Args:
            scenario: The scenario being scheduled.
            generator: Produces each trip's range-valid feasible plans.
            simulator: Turns a (partial or full) plan assignment into a schedule.
            cost_function: Scores a simulated schedule; lower is better.

        Returns:
            Exactly one chosen ``ChargingPlan`` for every trip in the scenario.
        """
