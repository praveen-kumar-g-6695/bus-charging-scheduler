"""The RESULT data model -- what the scheduler PRODUCES.

Where ``models.py`` describes the INPUT world (routes, buses, trips), this module
describes the OUTPUT: a concrete answer that says, for every trip, exactly when it
charged and how long it waited. Keeping input and output in separate modules is
deliberate -- the simulator's whole job is the one-way transform::

    Scenario (input)  --simulate-->  ScheduleResult (output)

and the cost function's job is the next one-way transform::

    ScheduleResult (output)  --score-->  one number

Because the two data shapes are decoupled, we can swap one scheduler for another
without touching either shape.

DESIGN NOTES
------------
* Every result model is FROZEN, exactly like the domain models. A result is a
  snapshot of one run; nothing should mutate it after the simulator hands it back.
* REFERENCE, DO NOT COPY. A result holds the actual input objects it describes
  (``trip``, ``station``, ``scenario``) instead of copying out their ids and
  names. Everything about a bus or charger is read THROUGH those references
  (``event.trip.bus.operator``, ``event.station.name``), so a result can never
  disagree with the input it came from. (A flattened, self-contained DTO is the
  right shape only at a serialization boundary such as a public REST API; these
  in-process frozen value objects are not that, so they reference instead.)
* The SINGLE SOURCE OF TRUTH is ``ScheduleResult.trip_timelines`` -- the per-trip
  view. The per-station view (``StationSchedule``) is DERIVED from it on demand,
  not stored, so the two views can never drift out of sync.
* Derived quantities (a charge's end and wait, a trip's total wait, the
  schedule's worst wait) are computed properties / methods, not stored fields.
  There is nothing to keep consistent because there is nothing duplicated.
"""

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.enums import Operator
from app.domain.models import Scenario, Station, Trip
from app.logging_config import get_logger

logger = get_logger(__name__)


class ChargeEvent(BaseModel):
    """One charging session: a trip filling up at one station.

    Following the same "reference, do not copy" rule as the domain models, this
    holds the actual ``trip`` and ``station`` objects rather than copying out
    their ids/names. Anything about the bus or the charger is read THROUGH them
    (``trip.bus.operator``, ``station.name``), so a result can never disagree with
    the input it came from.

    The two stored time fields tell the whole story of a single stop:

    * ``arrival_minute`` -- when the bus reaches the station.
    * ``start_minute``   -- when it actually starts charging. If the charger is
      busy the bus waits, so ``start_minute >= arrival_minute``; the gap is the
      wait.

    ``end_minute`` is NOT stored: charging always runs for the station's own
    ``charge_minutes``, so the end is derived. ``wait_minutes`` is likewise
    derived as ``start - arrival``.
    """

    model_config = ConfigDict(frozen=True)

    trip: Trip
    station: Station
    arrival_minute: int = Field(ge=0, description="when the bus reaches the station")
    start_minute: int = Field(ge=0, description="when charging begins")

    @model_validator(mode="after")
    def start_must_not_precede_arrival(self) -> "ChargeEvent":
        """Validate that charging does not begin before the bus has arrived.

        Returns:
            The validated charge event.

        Raises:
            ValueError: If charging starts before arrival.
        """
        if self.start_minute < self.arrival_minute:
            raise ValueError(
                f"charging cannot start (min {self.start_minute}) before arrival "
                f"(min {self.arrival_minute}) at {self.station.name}"
            )
        return self

    @property
    def end_minute(self) -> int:
        """Return when charging finishes.

        Derived from the station's own fill time, so it can never disagree with
        the charger's configured ``charge_minutes``.

        Returns:
            ``start_minute`` plus the station's ``charge_minutes``.
        """
        return self.start_minute + self.station.charge_minutes

    @property
    def wait_minutes(self) -> int:
        """Return how long the bus queued before charging.

        Returns:
            The minutes spent waiting, ``start_minute - arrival_minute``.
        """
        return self.start_minute - self.arrival_minute


class TripTimeline(BaseModel):
    """One trip's full journey outcome: every charge it made, and when it arrived.

    This is the per-bus view of the answer: what happened to THIS trip from
    departure to final arrival. It holds the ``trip`` itself (so its bus,
    operator, direction and departure are read through it), plus the charge events
    in the order the bus reached them along its direction of travel.
    """

    model_config = ConfigDict(frozen=True)

    trip: Trip
    charge_events: tuple[ChargeEvent, ...]
    arrival_minute: int = Field(ge=0, description="final arrival at the destination")

    @property
    def total_wait_minutes(self) -> int:
        """Return the total time this trip spent waiting across all its stops.

        Returns:
            The sum of ``wait_minutes`` over every charge event (0 if it never
            charged).
        """
        total = 0
        for event in self.charge_events:
            total += event.wait_minutes
        return total


class StationSchedule(BaseModel):
    """One charger's occupancy: every charging session that happened there.

    This is the per-station view. It holds the actual ``station`` object and is
    DERIVED from the trip timelines (see ``ScheduleResult.station_schedules``)
    rather than stored independently, so it can never disagree with the per-trip
    view. Its events are ordered by when charging started.
    """

    model_config = ConfigDict(frozen=True)

    station: Station
    events: tuple[ChargeEvent, ...]


class ScheduleResult(BaseModel):
    """The whole answer for one scheduler run.

    Holds the ``scenario`` it answers (so route, weights and trips are reachable
    without copying) and the per-trip timelines as the single source of truth. The
    per-station view and the summary metrics are computed on demand from the
    timelines, so there is nothing to keep in sync.
    """

    model_config = ConfigDict(frozen=True)

    scenario: Scenario
    trip_timelines: tuple[TripTimeline, ...]

    def all_charge_events(self) -> list[ChargeEvent]:
        """Flatten every charge event from every trip into one list.

        Returns:
            All charge events across all trips, in no particular order.
        """
        events: list[ChargeEvent] = []
        for timeline in self.trip_timelines:
            for event in timeline.charge_events:
                events.append(event)
        return events

    def station_schedules(self) -> list[StationSchedule]:
        """Derive the per-station view from the per-trip timelines.

        Groups every charge event by its station and sorts each station's events
        by when charging started, so each StationSchedule reads like that
        charger's timeline for the run.

        Returns:
            One StationSchedule per station that saw at least one charge,
            ordered by station name.
        """
        events_by_station: dict[str, list[ChargeEvent]] = {}
        station_by_name: dict[str, Station] = {}
        for event in self.all_charge_events():
            events_by_station.setdefault(event.station.name, []).append(event)
            station_by_name[event.station.name] = event.station

        schedules: list[StationSchedule] = []
        for station_name in sorted(events_by_station.keys()):
            station_events = events_by_station[station_name]
            station_events.sort(key=lambda one_event: one_event.start_minute)
            schedules.append(
                StationSchedule(
                    station=station_by_name[station_name],
                    events=tuple(station_events),
                )
            )
        return schedules

    def total_wait_minutes(self) -> int:
        """Return the total waiting time summed over every trip.

        Returns:
            The sum of each trip's ``total_wait_minutes``.
        """
        total = 0
        for timeline in self.trip_timelines:
            total += timeline.total_wait_minutes
        return total

    def max_wait_minutes(self) -> int:
        """Return the single worst wait any one trip suffered.

        Returns:
            The largest per-trip total wait, or 0 when there are no trips.
        """
        worst = 0
        for timeline in self.trip_timelines:
            if timeline.total_wait_minutes > worst:
                worst = timeline.total_wait_minutes
        return worst

    def wait_by_operator(self) -> dict[Operator, int]:
        """Return the total waiting time grouped by operator.

        This is the raw material the operator-fairness rule will weigh later.

        Returns:
            A map from operator to the total wait of all that operator's trips.
        """
        totals: dict[Operator, int] = {}
        for timeline in self.trip_timelines:
            operator = timeline.trip.bus.operator
            current = totals.get(operator, 0)
            totals[operator] = current + timeline.total_wait_minutes
        return totals
