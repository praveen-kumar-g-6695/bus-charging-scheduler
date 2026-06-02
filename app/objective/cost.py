"""The composite cost function: one weighted sum over every registered rule.

``CostFunction`` is the single objective the schedulers minimise. It is a
COMPOSITE: it holds the list of rules and, asked to score a schedule, it adds up
each rule's raw cost times that rule's per-scenario weight::

    total = sum(weight[rule.key] * rule.cost(schedule)  for rule in rules)

Two design choices make it the assignment's "how do weights / new rules work?"
answer in code form:

* It pulls its rules from the REGISTRY, not a hand-written list, so a newly
  ``@register_rule``-d objective is automatically included with zero edits here.
* It reads weights through ``Weights.for_rule(key)``, which returns 0.0 for any
  unset key, so a rule with no configured weight simply contributes nothing --
  adding or muting an objective is pure scenario data, never a code change.
"""

from app.domain.models import Weights
from app.domain.results import ScheduleResult
from app.logging_config import get_logger
from app.rules.base import Rule, RuleContext
from app.rules.registry import get_registry

logger = get_logger(__name__)


class CostFunction:
    """Scores a schedule as the weighted sum of every rule's raw cost.

    Constructed once from a scenario's ``Weights`` (and, by default, every rule in
    the shared registry). ``score`` is a pure read over a finished
    ``ScheduleResult``, so the schedulers can call it as often as they like while
    comparing candidate plans.
    """

    def __init__(self, weights: Weights, rules: list[Rule] | None = None) -> None:
        """Build a cost function from the scenario weights and a set of rules.

        Args:
            weights: The per-scenario weights; each rule's contribution is scaled
                by ``weights.for_rule(rule.key)``.
            rules: The rules to combine. Defaults to one instance of every rule in
                the shared registry, which is what production uses; an explicit
                list is mainly a testing seam.
        """
        self._weights = weights
        if rules is None:
            self._rules = get_registry().create_all()
        else:
            self._rules = rules

    def score(self, result: ScheduleResult) -> float:
        """Return the total weighted cost of a finished schedule.

        Lower is better. Each rule's raw cost is multiplied by its configured
        weight (0.0 when unset, so the rule drops out) and the products are summed.

        Args:
            result: The finished schedule to score.

        Returns:
            The weighted-sum cost across every rule.
        """
        context = RuleContext(result=result)

        total_cost = 0.0
        for rule in self._rules:
            raw_cost = rule.cost(context)
            weight = self._weights.for_rule(rule.key)
            weighted_cost = raw_cost * weight
            total_cost += weighted_cost
            logger.debug(f"rule '{rule.key}': raw {raw_cost} x weight {weight} = {weighted_cost}")

        logger.debug(f"total weighted cost = {total_cost}")
        return total_cost
