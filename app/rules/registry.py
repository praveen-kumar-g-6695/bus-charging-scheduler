"""The rule registry and the ``@register_rule`` decorator.

This is the open/closed seam of the scoring system. Rule classes announce
themselves by decorating with ``@register_rule``; the cost function later asks the
registry for "every known rule" and scores them all. Nothing keeps a hand-written
list of rules in sync, so adding an objective is a one-line decoration and zero
edits anywhere else.

The registry is a module-level singleton because the set of rule TYPES is global
program knowledge (it does not vary per scenario -- only the WEIGHTS do). A class
is registered exactly once, at import time, when its module is loaded.
"""

from app.logging_config import get_logger
from app.rules.base import Rule

logger = get_logger(__name__)


class RuleRegistry:
    """A name-keyed collection of the rule classes known to the program.

    Maps each rule's ``key`` to its class. The cost function instantiates every
    registered rule and scores them, so a rule that is registered is automatically
    part of the objective -- no central list to maintain.
    """

    def __init__(self) -> None:
        """Create an empty registry."""
        self._rule_classes: dict[str, type[Rule]] = {}

    def register(self, rule_class: type[Rule]) -> type[Rule]:
        """Add a rule class to the registry under its own ``key``.

        Args:
            rule_class: The ``Rule`` subclass to register.

        Returns:
            The same class unchanged, so this can be used as a decorator.

        Raises:
            ValueError: If another class is already registered under the same key.
        """
        rule_key = rule_class().key
        if rule_key in self._rule_classes:
            raise ValueError(f"a rule is already registered under key '{rule_key}'")
        self._rule_classes[rule_key] = rule_class
        logger.debug(f"registered rule '{rule_key}' -> {rule_class.__name__}")
        return rule_class

    def create_all(self) -> list[Rule]:
        """Instantiate one of every registered rule.

        Returns:
            A fresh instance of each registered rule class, ordered by key for
            deterministic scoring and logs.
        """
        rules: list[Rule] = []
        for rule_key in sorted(self._rule_classes.keys()):
            rules.append(self._rule_classes[rule_key]())
        return rules


# The single shared registry. Importing a module that defines rules populates it.
_REGISTRY = RuleRegistry()


def register_rule(rule_class: type[Rule]) -> type[Rule]:
    """Class decorator that registers a rule with the shared registry.

    Args:
        rule_class: The ``Rule`` subclass being defined.

    Returns:
        The same class, so it can be used directly as ``@register_rule``.
    """
    return _REGISTRY.register(rule_class)


def get_registry() -> RuleRegistry:
    """Return the shared rule registry.

    Returns:
        The module-level ``RuleRegistry`` singleton.
    """
    return _REGISTRY
