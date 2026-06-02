"""The domain data model as Pydantic value objects.

DESIGN NOTES
------------
* Every model is FROZEN (immutable). Once a Scenario is loaded it cannot be
  mutated by accident; the scheduler reads it and produces NEW result objects.
  Immutability removes a whole class of "who changed this?" bugs.
* Validation happens HERE, at the boundary. If a YAML file has a negative range
  or stations out of order, construction fails loudly with a clear message --
  the rest of the code can then trust the data completely.
* The single most important method is Route.station_sequence(direction). It is
  what makes the app handle BOTH travel directions from ONE route definition.

ENTITY OWNERSHIP (see ARCHITECTURE.md Decisions 13 & 14)
--------------------------------------------------------
Each fact lives on the entity that truly owns it, so the model scales and many
"anticipated changes" become config-only edits:
  * Station  -- a charger's physical IDENTITY: its name, how many charger lanes,
    and its charge_minutes (fill time). It carries NO position.
  * RouteStop-- the (route, station) association: a station PLUS its position_km
    along this particular route. Position is route-relative, so it lives here --
    the same Station can appear in two routes' stops at different positions.
  * Route    -- the road: origin/destination city names + an ordered list of stops.
  * Bus      -- the vehicle's physics only: range_km and speed_kmph.
  * Trip     -- one scheduled run: a bus + a direction + a departure_minute (this
    is INPUT timetable data, distinct from the charging schedule we PRODUCE).
  * Scenario -- the whole situation: route + weights + the list of trips.

POSITION CONVENTION
-------------------
We store every stop's position as kilometres from the route's ORIGIN end. The
origin is km 0, the destination is km total_length_km. A REVERSE trip starting at
the destination sees those same stops from the other side, so its
distance-from-origin is (total_length - position). station_sequence() does that
flip for us.
"""

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domain.enums import Direction, Operator
from app.logging_config import get_logger

logger = get_logger(__name__)


class Station(BaseModel):
    """One charging station -- its physical identity, with no route position.

    A station is a physical place: it has a name, some number of parallel charger
    lanes, and a charge_minutes fill time. It deliberately carries NO position,
    because "distance from origin" only has meaning relative to a particular route
    -- that lives on RouteStop. Keeping a station position-free lets the SAME
    station be shared by several routes later (Decision 14).
    """

    model_config = ConfigDict(frozen=True)

    name: str
    chargers: int = Field(default=1, ge=1, description="parallel charger lanes")
    charge_minutes: int = Field(default=25, gt=0, description="charger fill time")


class RouteStop(BaseModel):
    """A station placed at a position along ONE route.

    This is the (route, station) association: it pairs a physical Station with its
    position_km on this specific route. The position is route-relative, so it
    belongs here and not on the Station -- the same Station can sit at km 100 on
    one route and km 380 on another.
    """

    model_config = ConfigDict(frozen=True)

    station: Station
    position_km: int = Field(gt=0, description="km from this route's origin")


class Route(BaseModel):
    """The fixed road from origin to destination with its ordered stops.

    The endpoints (origin at km 0, destination at km total_length_km) are NOT
    stops -- buses leave the endpoints fully charged, so the endpoints never need
    scheduling. Only the in-between stops are. The actual city names live here
    (origin/destination) rather than in the Direction enum, so the app scales to
    any number of routes without code changes.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    origin: str
    destination: str
    total_length_km: int = Field(gt=0)
    stops: tuple[RouteStop, ...]

    @field_validator("stops")
    @classmethod
    def stops_must_be_sorted_and_unique(
        cls, given_stops: tuple[RouteStop, ...]
    ) -> tuple[RouteStop, ...]:
        """Validate that stops are in ascending km order and all distinct.

        We rely on this ordering everywhere downstream, so we prove it once here.

        Args:
            given_stops: The route stops as supplied to the model.

        Returns:
            The same stops, unchanged, once validated.

        Raises:
            ValueError: If the stops are not in ascending km order, or if two
                stops share the same position.
        """
        positions = []
        for stop in given_stops:
            positions.append(stop.position_km)

        sorted_positions = sorted(positions)
        if positions != sorted_positions:
            raise ValueError(f"stops must be in ascending km order, got {positions}")
        if len(set(positions)) != len(positions):
            raise ValueError(f"two stops share a position: {positions}")
        return given_stops

    @model_validator(mode="after")
    def stops_must_lie_within_route(self) -> "Route":
        """Validate that every stop sits strictly between the two endpoints.

        Returns:
            The validated route instance.

        Raises:
            ValueError: If any stop is at or beyond the route's end km.
        """
        for stop in self.stops:
            if stop.position_km >= self.total_length_km:
                raise ValueError(
                    f"station {stop.station.name} at km {stop.position_km} is not "
                    f"before the end at km {self.total_length_km}"
                )
        return self

    def station_sequence(self, direction: Direction) -> list[tuple[Station, int]]:
        """Return the stations a trip meets, in travel order, with origin distances.

        This is the bidirectional core of the whole app. The charger is the same
        physical station in both directions; only the order and the
        distance-from-origin differ.

        * FORWARD (origin -> destination): origin is km 0. The trip meets the
          stops in stored order, and distance-from-origin == ``position_km``.
        * REVERSE (destination -> origin): origin of travel is km
          ``total_length_km``. The trip meets the stops in reverse order, and
          distance-from-origin == ``total_length_km - position_km``.

        Example (total 540; A, B, C, D at 100, 220, 320, 440)::

            FORWARD -> [(A, 100), (B, 220), (C, 320), (D, 440)]
            REVERSE -> [(D, 100), (C, 220), (B, 320), (A, 440)]

        Args:
            direction: The direction the trip is travelling.

        Returns:
            A list of ``(station, distance_from_origin_km)`` tuples in the order
            the trip reaches them.
        """
        sequence: list[tuple[Station, int]] = []

        if direction == Direction.FORWARD:
            for stop in self.stops:
                distance_from_origin = stop.position_km
                sequence.append((stop.station, distance_from_origin))
        else:
            # Walk the stops from the destination end backwards.
            reversed_stops = list(reversed(self.stops))
            for stop in reversed_stops:
                distance_from_origin = self.total_length_km - stop.position_km
                sequence.append((stop.station, distance_from_origin))

        readable = [(one_station.name, dist) for one_station, dist in sequence]
        logger.debug(f"station_sequence({direction.value}) -> {readable}")
        return sequence


class Bus(BaseModel):
    """A physical bus -- the vehicle, holding only its own physics.

    A bus carries the properties that belong to the vehicle itself: its battery
    range and its cruising speed. It deliberately does NOT carry a direction or a
    departure time, because those describe a particular RUN of the bus, not the
    bus -- that is what Trip is for. Keeping range/speed here means "make this bus
    long-range" is a pure data edit.
    """

    model_config = ConfigDict(frozen=True)

    bus_id: str
    operator: Operator
    range_km: int = Field(default=240, gt=0, description="battery range in km")
    speed_kmph: int = Field(default=60, gt=0, description="cruising speed")

    def travel_minutes_for_km(self, distance_km: int) -> int:
        """Convert a driving distance into minutes at this bus's speed.

        At 60 km/h, 1 km takes exactly 1 minute, which keeps the demo numbers
        clean -- but we compute it from speed so a different speed just works.

        Args:
            distance_km: The distance to drive, in kilometres.

        Returns:
            The travel time in whole minutes, rounded to the nearest minute.
        """
        minutes = round(distance_km * 60 / self.speed_kmph)
        return minutes


class Trip(BaseModel):
    """One scheduled run of a bus: which bus, which way, leaving when.

    This is the INPUT timetable entry (the "given" in the assignment), and it is
    deliberately separate from the charging schedule we PRODUCE. Direction and
    departure live here -- on the run -- not on the physical Bus, because the same
    bus could run a different direction or time on another day.

    departure_minute is minutes from a common reference (e.g. 19:00 == 0). Storing
    an int, not a clock string, keeps all arithmetic trivial; the loader converts
    "19:15" -> 15 and the UI can convert back for display.
    """

    model_config = ConfigDict(frozen=True)

    bus: Bus
    direction: Direction
    departure_minute: int = Field(ge=0)


class Weights(BaseModel):
    """The tunable knobs, kept in ONE obvious place.

    Stored as a plain name->number map so that adding a brand-new weight (for a
    brand-new rule) is just another key here / in the YAML -- no new field, no
    code change. The three the assignment names default to 1.0.

    for_rule(key) returns the weight for a rule, defaulting to 0.0 when a rule has
    no weight set (so an unweighted rule simply contributes nothing).
    """

    model_config = ConfigDict(frozen=True)

    values: dict[str, float] = Field(
        default_factory=lambda: {
            "individual": 1.0,
            "operator": 1.0,
            "overall": 1.0,
        }
    )

    def for_rule(self, rule_key: str) -> float:
        """Look up the weight for a rule key.

        Args:
            rule_key: The registry key of the rule (for example ``"operator"``).

        Returns:
            The configured weight, or ``0.0`` when the key is absent so an
            unweighted rule simply contributes nothing.
        """
        weight = self.values.get(rule_key, 0.0)
        return weight


class Scenario(BaseModel):
    """A complete, self-describing situation the scheduler can run.

    A scenario IS the data structure: it carries the route, the tunable weights,
    and the list of trips (the input timetable). Everything the scheduler needs to
    produce a schedule comes from here -- nothing is hardcoded in the engine.

    The physical constants are NOT on the scenario: range/speed belong to each Bus
    and charge_minutes belongs to each Station (see ARCHITECTURE.md Decision 13),
    so a future scenario can model a longer-range fleet or a faster charger purely
    as data.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    route: Route
    weights: Weights
    trips: tuple[Trip, ...]
    reference_minutes: int = Field(
        default=0,
        ge=0,
        lt=24 * 60,
        description="clock anchor in minutes-since-midnight; all trip/event minutes "
        "are relative to this, so the UI can render real wall-clock times",
    )
