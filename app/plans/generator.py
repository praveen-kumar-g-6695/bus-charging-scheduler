"""Direction-aware generation of every range-valid charging plan for a trip.

A trip leaves its origin with a full battery and must reach its destination
without ever driving more than its bus's range between two consecutive charges
(the origin and destination count as "full" endpoints). This module enumerates
exactly the plans that satisfy that rule.

The single subtlety -- and the reason this is more than ``main.py``'s version --
is DIRECTION. A REVERSE trip meets the stations in the opposite order and at
different distances-from-origin. We never special-case that here: we simply ask
``Route.station_sequence(direction)`` for the stations already in travel order
with their distances, and the rest of the logic is direction-agnostic.
"""

from itertools import combinations

from pydantic import BaseModel, ConfigDict

from app.domain.models import Route, Station, Trip
from app.logging_config import get_logger

logger = get_logger(__name__)


class ChargingPlan(BaseModel):
    """One candidate set of stations where a trip charges, in travel order.

    A plan is just the ordered stations chosen for charging. It is range-valid by
    construction (the generator only ever emits legal plans), and frozen like
    every other value object so it can be shared freely.
    """

    model_config = ConfigDict(frozen=True)

    stations: tuple[Station, ...]

    @property
    def station_names(self) -> tuple[str, ...]:
        """Return just the station names, in travel order.

        Returns:
            A tuple of the chosen stations' names (empty if the plan charges
            nowhere).
        """
        names = []
        for station in self.stations:
            names.append(station.name)
        return tuple(names)


class PlanGenerator:
    """Enumerates the range-valid charging plans for trips on one route.

    Constructed once per route. Holds no per-trip state, so the same generator
    can produce plans for every trip in a scenario.
    """

    def __init__(self, route: Route) -> None:
        """Store the route whose geometry the plans are built against.

        Args:
            route: The route every generated plan is measured along.
        """
        self._route = route

    def feasible_plans(self, trip: Trip) -> list[ChargingPlan]:
        """Return every charging plan this trip could legally use.

        Walks the trip's stations in travel order and tries every subset of them
        as charging stops, keeping only those where no leg (origin -> first
        charge -> ... -> destination) exceeds the bus's range.

        Args:
            trip: The trip to generate plans for; its direction fixes the station
                order and its bus fixes the range.

        Returns:
            All range-valid plans, ordered from fewest charges to most.
        """
        sequence = self._route.station_sequence(trip.direction)
        range_km = trip.bus.range_km
        total_length = self._route.total_length_km
        bus_id = trip.bus.bus_id

        logger.debug(
            f"generating feasible plans for {bus_id} ({trip.direction.value}), "
            f"range {range_km} km over {total_length} km"
        )

        valid_plans: list[ChargingPlan] = []

        # Try every possible NUMBER of charges, from 0 up to "charge everywhere".
        for number_of_charges in range(len(sequence) + 1):
            # combinations keeps the stations in travel order, so each chosen
            # tuple is already a properly ordered candidate plan.
            for chosen in combinations(sequence, number_of_charges):
                longest_gap = self._longest_gap(chosen, total_length)

                if longest_gap <= range_km:
                    chosen_stations = []
                    for station, _distance in chosen:
                        chosen_stations.append(station)
                    plan = ChargingPlan(stations=tuple(chosen_stations))
                    valid_plans.append(plan)
                    logger.debug(f"  VALID  {plan.station_names} -- longest leg {longest_gap} km")
                else:
                    reject_names = []
                    for station, _distance in chosen:
                        reject_names.append(station.name)
                    logger.debug(
                        f"  reject {tuple(reject_names)} -- longest leg "
                        f"{longest_gap} km > range {range_km} km"
                    )

        logger.debug(f"{bus_id}: {len(valid_plans)} feasible plan(s)")
        return valid_plans

    def _longest_gap(self, chosen: tuple[tuple[Station, int], ...], total_length: int) -> int:
        """Return the longest leg the bus drives under a candidate plan.

        Builds the full list of points the bus passes -- origin (km 0), each
        chosen charging station's distance, then the destination -- and measures
        the biggest gap between consecutive points.

        Args:
            chosen: The chosen ``(station, distance_from_origin)`` pairs, already
                in travel order.
            total_length: The route length, i.e. the destination's distance.

        Returns:
            The length in km of the longest single leg.
        """
        points = [0]
        for _station, distance in chosen:
            points.append(distance)
        points.append(total_length)

        longest_gap = 0
        for index in range(len(points) - 1):
            gap = points[index + 1] - points[index]
            if gap > longest_gap:
                longest_gap = gap
        return longest_gap
