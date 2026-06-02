"""A single physical charging station and its parallel charger lanes.

One ``ChargerStation`` models the real hardware at one place on the road: a fixed
number of charger lanes (``Station.chargers``) that buses from EITHER direction
queue for. It is the one piece of mutable state in the simulation -- it remembers
when each lane next falls free -- so it lives in its own small class rather than
being smeared across the simulator.

The contract is deliberately tiny: a bus says "I arrived at minute N", and the
station replies "you may start charging at minute M" (M >= N), having reserved a
lane until M + charge_minutes. First-come-first-served falls out naturally: serve
buses in arrival order and each takes the lane that frees up earliest.
"""

from app.domain.models import Station
from app.logging_config import get_logger

logger = get_logger(__name__)


class ChargerStation:
    """Tracks lane availability at one station and assigns charging start times.

    Holds one "next free at" clock per charger lane. ``request`` is the only way
    to advance that state: it picks the lane that frees up soonest, starts the bus
    as soon as both it and a lane are ready, and reserves that lane for the
    station's fill time.
    """

    def __init__(self, station: Station) -> None:
        """Create a station with all of its lanes initially free at minute 0.

        Args:
            station: The physical station being modelled; its ``chargers`` count
                fixes the number of lanes and its ``charge_minutes`` the fill
                time.
        """
        self._station = station
        self._lane_free_at: list[int] = []
        for _lane_index in range(station.chargers):
            self._lane_free_at.append(0)

    def _earliest_lane(self) -> tuple[int, int]:
        """Return the lane that frees up soonest and when it does.

        Returns:
            A ``(lane_index, free_minute)`` pair for the earliest-free lane.
        """
        earliest_lane_index = 0
        earliest_free = self._lane_free_at[0]
        for lane_index in range(1, len(self._lane_free_at)):
            if self._lane_free_at[lane_index] < earliest_free:
                earliest_free = self._lane_free_at[lane_index]
                earliest_lane_index = lane_index
        return earliest_lane_index, earliest_free

    def peek(self, arrival_minute: int) -> int:
        """Return when a bus arriving now COULD start, without reserving a lane.

        This is the read-only twin of ``request``: it answers "if this bus
        charged here, when would it begin?" while leaving the station's state
        untouched. The selfish greedy uses it to predict a trip's own wait across
        a candidate plan before committing to anything.

        Args:
            arrival_minute: The minute the bus reaches this station.

        Returns:
            The minute the bus would start charging (>= ``arrival_minute``).
        """
        _lane_index, earliest_free = self._earliest_lane()
        return max(arrival_minute, earliest_free)

    def request(self, arrival_minute: int) -> int:
        """Reserve a lane for a bus arriving now and return when it can charge.

        Picks the lane that becomes free earliest. The bus starts when both it has
        arrived and that lane is free, i.e. ``max(arrival, earliest_free)``; the
        lane is then held until charging ends. Serving buses in arrival order and
        always taking the earliest-free lane is exactly first-come-first-served
        across however many lanes the station has.

        Args:
            arrival_minute: The minute the bus reaches this station.

        Returns:
            The minute the bus actually starts charging (>= ``arrival_minute``).
        """
        earliest_lane_index, earliest_free = self._earliest_lane()

        start_minute = max(arrival_minute, earliest_free)
        end_minute = start_minute + self._station.charge_minutes
        self._lane_free_at[earliest_lane_index] = end_minute

        wait_minutes = start_minute - arrival_minute
        logger.debug(
            f"{self._station.name}: bus arrived {arrival_minute}, lane "
            f"{earliest_lane_index} free at {earliest_free} -> start {start_minute} "
            f"(wait {wait_minutes}), held until {end_minute}"
        )
        return start_minute
