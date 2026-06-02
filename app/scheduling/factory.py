"""A tiny Factory that builds a scheduler by NAME, defaulting to local search.

The app has several interchangeable ``SchedulerStrategy`` implementations (two
greedy variants and a local search that refines a greedy seed). Everything that
just wants "a scheduler" -- the CLI, the Streamlit UI, tests -- should ask here by
name instead of importing and wiring a concrete class. That keeps the choice of
algorithm in ONE obvious place and makes swapping it a one-liner, which is exactly
the flexibility we want to show live.

Default is ``"local_search"`` (a local search seeded by the global greedy): the
highest-quality option we have, and the one the UI should use. The benchmark
showed the global-greedy seed dominates the selfish-greedy seed, so local search
refines the stronger start.

Adding a new strategy later is a two-line change here (a builder + a registry
entry); no caller has to change.
"""

from collections.abc import Callable

from app.logging_config import get_logger
from app.scheduling.base import SchedulerStrategy
from app.scheduling.greedy import GlobalGreedyScheduler, SelfishGreedyScheduler
from app.scheduling.local_search import LocalSearchScheduler

logger = get_logger(__name__)

DEFAULT_SCHEDULER = "local_search"

# name -> zero-arg builder. Builders (not instances) so each call gets a fresh,
# independent scheduler, and so construction stays lazy.
_BUILDERS: dict[str, Callable[[], SchedulerStrategy]] = {
    "selfish_greedy": SelfishGreedyScheduler,
    "global_greedy": GlobalGreedyScheduler,
    "local_search": lambda: LocalSearchScheduler(seed=GlobalGreedyScheduler()),
}


def available_schedulers() -> tuple[str, ...]:
    """Return the names that ``create_scheduler`` accepts.

    Returns:
        The registered scheduler names, sorted for stable display.
    """
    return tuple(sorted(_BUILDERS))


def create_scheduler(name: str = DEFAULT_SCHEDULER) -> SchedulerStrategy:
    """Build a scheduler by name, defaulting to the local search.

    Args:
        name: One of ``available_schedulers``; defaults to ``"local_search"``.

    Returns:
        A freshly built ``SchedulerStrategy``.

    Raises:
        ValueError: If ``name`` is not a registered scheduler.
    """
    builder = _BUILDERS.get(name)
    if builder is None:
        known = ", ".join(available_schedulers())
        raise ValueError(f"unknown scheduler '{name}'; choose one of: {known}")

    scheduler = builder()
    logger.debug(f"factory built scheduler '{name}' -> {scheduler.__class__.__name__}")
    return scheduler
