"""The event-driven scheduler simulator.

Given a scenario and one chosen ``ChargingPlan`` per trip, this plays the clock
forward and reports exactly what happens: when each bus reaches each station, how
long it queues, when it charges, and when it finally arrives.

WHY EVENT-DRIVEN (and not just "process buses in departure order")
------------------------------------------------------------------
The chargers are shared, so the only fair, correct rule is first-come-first-
served by ARRIVAL TIME at each station -- and a forward bus and a reverse bus can
contend for the very same physical charger (Decision 13). A bus's arrival at its
next station, though, depends on how long it queued at the previous one, so we
cannot know all arrival times up front.

So we run a tiny discrete-event loop. A min-heap always hands us the next bus to
arrive *anywhere*, we charge it (which is the only moment a lane gets reserved),
then we compute and push that bus's arrival at its following station. Processing
strictly in arrival order is what makes the shared-charger contention correct.
"""

import heapq

from app.domain.models import Scenario, Station, Trip
from app.domain.results import ChargeEvent, ScheduleResult, TripTimeline
from app.logging_config import get_logger
from app.plans.generator import ChargingPlan
from app.simulation.charger import ChargerStation

logger = get_logger(__name__)


class _TripProgress:
    """Mutable bookkeeping for one trip as the simulation plays out.

    This is private scratch state, not a value object, so it is a plain mutable
    class rather than a frozen model. It remembers where the trip is in its plan
    and accumulates the charge events that will become its ``TripTimeline``.
    """

    def __init__(self, trip: Trip, points: list[tuple[Station, int]]) -> None:
        """Set up a trip's progress at the moment it departs.

        Args:
            trip: The trip being simulated.
            points: The trip's charging stations in travel order, each paired with
                its distance-from-origin in km.
        """
        self.trip = trip
        self.points = points
        self.next_step = 0
        self.charge_events: list[ChargeEvent] = []


class ScheduleSimulator:
    """Plays a full assignment of plans forward into a concrete ScheduleResult.

    Constructed once per scenario; ``simulate`` is stateless between calls (it
    builds fresh charger stations each run), so the same simulator can score many
    different plan assignments -- which is exactly what the greedy and local-search
    schedulers will do.
    """

    def __init__(self, scenario: Scenario) -> None:
        """Store the scenario whose route and trips will be simulated.

        Args:
            scenario: The scenario providing the route geometry and the trips.
        """
        self._scenario = scenario

    def simulate(self, plan_by_trip: dict[Trip, ChargingPlan]) -> ScheduleResult:
        """Simulate every trip's chosen plan and return the concrete schedule.

        Args:
            plan_by_trip: The plan each trip will follow. Every trip in the
                scenario must appear as a key.

        Returns:
            A ``ScheduleResult`` whose trip timelines record the real charge
            start/end and wait at every stop, plus each trip's final arrival.
        """
        route = self._scenario.route
        total_length = route.total_length_km

        stations_by_name = self._build_charger_stations()
        progress_by_index = self._build_progress(plan_by_trip)

        # The event heap holds (arrival_minute, bus_id, trip_index): the next time
        # each still-travelling trip reaches a charging station. All three fields
        # are scalars, so ties break deterministically without comparing objects.
        event_heap: list[tuple[int, str, int]] = []
        final_arrival_by_index: dict[int, int] = {}

        for trip_index, progress in progress_by_index.items():
            self._schedule_first_arrival(
                trip_index, progress, total_length, event_heap, final_arrival_by_index
            )

        while event_heap:
            arrival_minute, _bus_id, trip_index = heapq.heappop(event_heap)
            progress = progress_by_index[trip_index]
            self._charge_then_advance(
                trip_index,
                progress,
                arrival_minute,
                total_length,
                stations_by_name,
                event_heap,
                final_arrival_by_index,
            )

        return self._build_result(progress_by_index, final_arrival_by_index)

    def _build_charger_stations(self) -> dict[str, ChargerStation]:
        """Create one shared ChargerStation per physical station on the route.

        Keying by station name means a forward bus and a reverse bus that reach
        the same place share the same lanes, which is the whole point of
        modelling the charger as physical hardware.

        Returns:
            A mapping from station name to its fresh ``ChargerStation``.
        """
        stations_by_name: dict[str, ChargerStation] = {}
        for stop in self._scenario.route.stops:
            stations_by_name[stop.station.name] = ChargerStation(stop.station)
        return stations_by_name

    def _build_progress(self, plan_by_trip: dict[Trip, ChargingPlan]) -> dict[int, _TripProgress]:
        """Build each trip's per-step charging points from its chosen plan.

        For each trip we look up its plan's stations' distances in that trip's own
        direction, so a reverse trip's points come out in reverse travel order
        automatically.

        Args:
            plan_by_trip: The chosen plan for every trip.

        Returns:
            A mapping from each trip's index (its position in the scenario) to its
            ``_TripProgress``.

        Raises:
            KeyError: If a trip in the scenario has no plan in ``plan_by_trip``.
        """
        progress_by_index: dict[int, _TripProgress] = {}
        for trip_index, trip in enumerate(self._scenario.trips):
            plan = plan_by_trip[trip]
            distance_by_name = self._distance_by_name(trip)

            points: list[tuple[Station, int]] = []
            for station in plan.stations:
                points.append((station, distance_by_name[station.name]))

            progress_by_index[trip_index] = _TripProgress(trip, points)
        return progress_by_index

    def _distance_by_name(self, trip: Trip) -> dict[str, int]:
        """Map each station's name to its distance-from-origin for this trip.

        Args:
            trip: The trip whose direction fixes the distances.

        Returns:
            A mapping from station name to distance-from-origin in km, in this
            trip's direction of travel.
        """
        distance_by_name: dict[str, int] = {}
        for station, distance in self._scenario.route.station_sequence(trip.direction):
            distance_by_name[station.name] = distance
        return distance_by_name

    def _schedule_first_arrival(
        self,
        trip_index: int,
        progress: _TripProgress,
        total_length: int,
        event_heap: list[tuple[int, str, int]],
        final_arrival_by_index: dict[int, int],
    ) -> None:
        """Push a trip's first event, or finish it now if it never charges.

        A trip that charges nowhere just drives straight to the destination, so it
        produces no events and its final arrival is recorded immediately.

        Args:
            trip_index: The trip's position in the scenario.
            progress: The trip's progress bookkeeping.
            total_length: The route length (the destination's distance).
            event_heap: The shared event heap to push the first arrival onto.
            final_arrival_by_index: Where a no-charge trip's arrival is recorded.
        """
        trip = progress.trip
        if not progress.points:
            final_arrival = trip.departure_minute + trip.bus.travel_minutes_for_km(total_length)
            final_arrival_by_index[trip_index] = final_arrival
            logger.debug(f"{trip.bus.bus_id}: no charging, arrives {final_arrival}")
            return

        _first_station, first_distance = progress.points[0]
        arrival_minute = trip.departure_minute + trip.bus.travel_minutes_for_km(first_distance)
        heapq.heappush(event_heap, (arrival_minute, trip.bus.bus_id, trip_index))

    def _charge_then_advance(
        self,
        trip_index: int,
        progress: _TripProgress,
        arrival_minute: int,
        total_length: int,
        stations_by_name: dict[str, ChargerStation],
        event_heap: list[tuple[int, str, int]],
        final_arrival_by_index: dict[int, int],
    ) -> None:
        """Charge a trip at its current station, then queue its next arrival.

        This is the body of the event loop for one popped arrival: reserve a lane
        (which is the only place a charger's state changes), record the charge
        event, then either push the arrival at the following station or, if this
        was the last charge, record the final arrival at the destination.

        Args:
            trip_index: The trip's position in the scenario.
            progress: The trip's progress bookkeeping (advanced by one step).
            arrival_minute: The minute the trip reached the current station.
            total_length: The route length (the destination's distance).
            stations_by_name: The shared charger stations, keyed by name.
            event_heap: The shared event heap to push the next arrival onto.
            final_arrival_by_index: Where a finished trip's arrival is recorded.
        """
        trip = progress.trip
        station, station_distance = progress.points[progress.next_step]

        start_minute = stations_by_name[station.name].request(arrival_minute)
        event = ChargeEvent(
            trip=trip,
            station=station,
            arrival_minute=arrival_minute,
            start_minute=start_minute,
        )
        progress.charge_events.append(event)
        charge_end = event.end_minute
        progress.next_step += 1

        if progress.next_step < len(progress.points):
            _next_station, next_distance = progress.points[progress.next_step]
            leg_km = next_distance - station_distance
            next_arrival = charge_end + trip.bus.travel_minutes_for_km(leg_km)
            heapq.heappush(event_heap, (next_arrival, trip.bus.bus_id, trip_index))
        else:
            leg_km = total_length - station_distance
            final_arrival = charge_end + trip.bus.travel_minutes_for_km(leg_km)
            final_arrival_by_index[trip_index] = final_arrival
            logger.debug(
                f"{trip.bus.bus_id}: last charge at {station.name}, arrives {final_arrival}"
            )

    def _build_result(
        self,
        progress_by_index: dict[int, _TripProgress],
        final_arrival_by_index: dict[int, int],
    ) -> ScheduleResult:
        """Assemble the per-trip timelines into the final ScheduleResult.

        Timelines are emitted in the scenario's trip order so the output lines up
        with the input timetable.

        Args:
            progress_by_index: Each trip's accumulated charge events.
            final_arrival_by_index: Each trip's final arrival minute.

        Returns:
            The completed ``ScheduleResult`` for this assignment.
        """
        timelines: list[TripTimeline] = []
        for trip_index in range(len(self._scenario.trips)):
            progress = progress_by_index[trip_index]
            timeline = TripTimeline(
                trip=progress.trip,
                charge_events=tuple(progress.charge_events),
                arrival_minute=final_arrival_by_index[trip_index],
            )
            timelines.append(timeline)

        return ScheduleResult(scenario=self._scenario, trip_timelines=tuple(timelines))
