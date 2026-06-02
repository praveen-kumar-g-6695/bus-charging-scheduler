"""Enumerations for the fixed, named choices in the domain.

We use real Enum classes (not bare strings) so that:
  * typos become errors at load time, not silent wrong behaviour later;
  * the set of valid values is documented in ONE place;
  * IDEs autocomplete them.

Both enums inherit from `str` as well as `Enum`. That little trick means a
Direction IS a string ("BK"), so it serialises straight to/from YAML and prints
nicely, while still being a proper typed enum in code.
"""

from enum import Enum


class Direction(str, Enum):
    """Which way along a route a trip travels.

    Direction is deliberately ROUTE-AGNOSTIC. The actual city names live on the
    Route (``origin`` and ``destination``), not here, so the app scales to any
    number of routes without ever touching this enum.

    * ``FORWARD`` -- travel from the route's origin to its destination.
    * ``REVERSE`` -- travel from the route's destination back to its origin.

    This single distinction is the whole reason the production app is more than
    ``main.py``: the stations a trip sees, and the distance to each, depend on
    the direction it is travelling.
    """

    FORWARD = "forward"
    REVERSE = "reverse"


class Operator(str, Enum):
    """The bus operating company a bus belongs to.

    Today there are three. The 'operator' soft rule cares that each company's
    fleet runs smoothly as a group. Adding a 4th operator later is just one more
    member here (and the rule keeps working unchanged).
    """

    KPN = "kpn"
    FRESHBUS = "freshbus"
    FLIXBUS = "flixbus"
