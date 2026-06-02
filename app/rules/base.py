"""The Rule abstraction and the small context object every rule is handed.

A ``Rule`` is one scoring objective: given a finished schedule, it returns a
single non-negative "badness" number (0 == perfect for this objective, higher ==
worse). Keeping every objective behind this one tiny interface is what lets the
cost function treat them uniformly and lets new objectives be added without the
engine ever knowing their internals.
"""

from abc import ABC, abstractmethod

from pydantic import BaseModel, ConfigDict

from app.domain.results import ScheduleResult


class RuleContext(BaseModel):
    """Everything a rule is allowed to look at when scoring, in one frozen object.

    Today a rule needs only the finished ``ScheduleResult`` (which itself reaches
    the scenario, trips and weights by reference). Wrapping it in a context rather
    than passing the raw result is a deliberate seam: if a future rule needs extra
    inputs (say external pricing, or precomputed metrics), they get added here once
    and every rule can use them -- without changing the ``Rule`` signature.
    """

    model_config = ConfigDict(frozen=True)

    result: ScheduleResult


class Rule(ABC):
    """One weightable scoring objective over a finished schedule.

    Concrete rules implement ``key`` (the name their weight is looked up under, in
    ``Weights`` and the YAML) and ``cost`` (the raw, UNWEIGHTED badness). Rules
    never see their own weight: the cost function multiplies it in afterwards, so a
    rule stays a pure, independently testable function of the schedule.
    """

    @property
    @abstractmethod
    def key(self) -> str:
        """Return the rule's weight key.

        Returns:
            The string used to look this rule's weight up in ``Weights`` and in
            scenario YAML (for example ``"individual"``).
        """

    @abstractmethod
    def cost(self, context: RuleContext) -> float:
        """Return the raw, unweighted badness of the schedule for this objective.

        Args:
            context: The scoring context wrapping the finished schedule.

        Returns:
            A non-negative number; 0 means perfect for this objective and larger
            means worse. It is intentionally unweighted -- the cost function
            applies the per-scenario weight.
        """
