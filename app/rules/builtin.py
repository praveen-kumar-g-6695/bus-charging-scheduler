"""The three built-in scoring rules named by the assignment.

Each rule reads only the finished schedule (via its ``RuleContext``) and returns a
raw badness in MINUTES, so the three are directly comparable and the default
weights of 1.0 each are meaningful. Importing this module is what registers them.

The three objectives, and how we make each a concrete number:

* ``individual`` -- protect the worst-off single bus. Scored as the largest wait
  any one trip suffers, so the optimiser is pushed to flatten the worst case.
* ``operator``   -- fairness BETWEEN operators. Scored as the spread (max minus
  min) of total wait across operators, so 0 means every operator waited equally.
* ``overall``    -- system-wide efficiency. Scored as the total wait summed over
  every trip, so it rewards reducing waiting everywhere at once.
"""

from app.domain.results import ScheduleResult
from app.rules.base import Rule, RuleContext
from app.rules.registry import register_rule


@register_rule
class IndividualWaitRule(Rule):
    """Penalises the worst single-trip wait (protects the individual bus).

    Using the maximum (not the average) makes this objective specifically about
    nobody being left waiting a long time, which is the "individual" concern --
    total/average waiting is what the overall rule already covers.
    """

    @property
    def key(self) -> str:
        """Return this rule's weight key.

        Returns:
            The string ``"individual"``.
        """
        return "individual"

    def cost(self, context: RuleContext) -> float:
        """Return the worst single-trip wait in the schedule.

        Args:
            context: The scoring context wrapping the finished schedule.

        Returns:
            The largest per-trip total wait, in minutes (0 if nobody waited).
        """
        result: ScheduleResult = context.result
        return float(result.max_wait_minutes())


@register_rule
class OperatorFairnessRule(Rule):
    """Penalises imbalance of waiting time between operators.

    Scored as the spread (max minus min) of per-operator total wait. A spread of 0
    means every operator's buses waited the same in aggregate; a large spread
    means one operator is bearing most of the delay. This is a single SYMMETRIC
    fairness knob -- it does not favour any particular operator (per-operator
    priority would be a separate rule + weight key, see ARCHITECTURE Decision 16).
    """

    @property
    def key(self) -> str:
        """Return this rule's weight key.

        Returns:
            The string ``"operator"``.
        """
        return "operator"

    def cost(self, context: RuleContext) -> float:
        """Return the spread of total wait across operators.

        Args:
            context: The scoring context wrapping the finished schedule.

        Returns:
            ``max - min`` of per-operator total wait in minutes; 0 when fewer than
            two operators ran (nothing to be unfair between).
        """
        result: ScheduleResult = context.result
        waits_by_operator = result.wait_by_operator()
        if len(waits_by_operator) < 2:
            return 0.0

        operator_waits = list(waits_by_operator.values())
        return float(max(operator_waits) - min(operator_waits))


@register_rule
class OverallRule(Rule):
    """Penalises total waiting across the whole system (overall efficiency).

    Scored as the sum of every trip's wait, so reducing waiting anywhere lowers it.
    This is the "get everyone moving" objective, distinct from the individual
    worst-case and the cross-operator fairness ones.
    """

    @property
    def key(self) -> str:
        """Return this rule's weight key.

        Returns:
            The string ``"overall"``.
        """
        return "overall"

    def cost(self, context: RuleContext) -> float:
        """Return the total wait summed over every trip.

        Args:
            context: The scoring context wrapping the finished schedule.

        Returns:
            The total wait across all trips, in minutes.
        """
        result: ScheduleResult = context.result
        return float(result.total_wait_minutes())
