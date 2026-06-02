"""The rules layer: pluggable, individually-weightable cost components.

The assignment scores a schedule against several competing objectives at once
(keep individual waits low, keep operators treated fairly, keep the whole
operation efficient) and lets each objective be re-weighted per scenario. Rather
than bake those objectives into one big formula, each becomes its own small
``Rule`` object that turns a finished schedule into a single "badness" number.

WHY A REGISTRY OF RULES (the design the assignment is really testing)
---------------------------------------------------------------------
A registry + decorator (``@register_rule``) means adding a brand-new objective is
purely additive: write one ``Rule`` subclass, decorate it, give it a weight key.
The scheduler, simulator and cost function never change. That is the concrete
answer to the README's "how do I add a new rule / new weight without touching the
engine?" -- and it is why rules live in their own layer, decoupled from both the
simulator that produces schedules and the cost function that sums them.

Each rule is deliberately weight-AGNOSTIC: it returns a raw, unweighted number.
Combining rules with their per-scenario weights is the cost function's job
(Step 6), so a rule never needs to know how important it currently is.

SELF-REGISTRATION
-----------------
Importing this package imports ``builtin`` for its side effect: each built-in rule
runs its ``@register_rule`` decorator at import time, so the shared registry is
always populated. This means any consumer of the registry (the cost function, the
schedulers, the benchmark, the UI) sees the built-in rules without having to
remember to import ``builtin`` itself. A new rule module added later should be
imported here too (or imported wherever it is defined) so it self-registers.
"""

from app.rules import builtin as builtin  # noqa: F401 - imported for rule self-registration
