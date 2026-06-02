"""The ScenarioLoader: turn a self-describing YAML file into a domain Scenario.

A scenario file IS the data structure (see ARCHITECTURE.md). Each YAML fully
describes one situation -- the route and its stations, the tunable weights, and
the timetable of trips -- so the scheduler reads ONLY data and hardcodes nothing.

This module is the one place that knows the file FORMAT. It parses the YAML, turns
human-friendly clock times ("19:15") into the relative ``departure_minute`` the
domain uses (Decision 15), and hands back fully-validated frozen domain objects.
If a file is malformed, construction fails loudly HERE at the boundary, so the
rest of the app can trust every Scenario it is given.

A scenario file looks like::

    name: "Scenario 1 - Even spacing"
    reference_time: "19:00"        # clock anchor; departures are clock times
    route:
      name: "Bengaluru-Kochi Highway"
      origin: "Bengaluru"
      destination: "Kochi"
      total_length_km: 540
      stops:
        - station: "A"
          position_km: 100         # chargers/charge_minutes default 1/25
        - station: "B"
          position_km: 220
    weights:
      individual: 1.0
      operator: 1.0
      overall: 1.0
    fleet_defaults:                 # optional per-file bus physics defaults
      range_km: 240
      speed_kmph: 60
    trips:
      - bus_id: "bus-BK-01"
        operator: "kpn"
        direction: "forward"        # forward = origin->destination
        departure: "19:00"
"""

from pathlib import Path
from typing import Any

import yaml

from app.domain.enums import Direction, Operator
from app.domain.models import Bus, Route, RouteStop, Scenario, Station, Trip, Weights
from app.logging_config import get_logger

logger = get_logger(__name__)


class ScenarioLoader:
    """Parses scenario YAML files into validated frozen ``Scenario`` objects.

    The loader is the single owner of the file FORMAT: callers give it a path and
    get back a domain ``Scenario``, never touching YAML themselves. It also owns
    the clock-time -> relative-minute conversion, keeping that concern out of the
    domain models (which only ever see integer minutes).
    """

    def load_file(self, path: str | Path) -> Scenario:
        """Load and validate a single scenario YAML file.

        Args:
            path: Path to the scenario ``.yaml`` file.

        Returns:
            The fully-built, validated ``Scenario``.

        Raises:
            FileNotFoundError: If the path does not exist.
            ValueError: If the file is empty or structurally invalid.
        """
        scenario_path = Path(path)
        logger.info(f"loading scenario file {scenario_path}")

        raw_text = scenario_path.read_text(encoding="utf-8")
        data = yaml.safe_load(raw_text)
        if not isinstance(data, dict):
            raise ValueError(f"{scenario_path} did not contain a scenario mapping")

        return self._build_scenario(data)

    def load_dir(self, directory: str | Path) -> list[Scenario]:
        """Load every ``*.yaml`` file in a directory, sorted by file name.

        Args:
            directory: Path to a folder of scenario files.

        Returns:
            The loaded scenarios, ordered by file name for a stable dropdown.

        Raises:
            FileNotFoundError: If the directory does not exist.
        """
        scenario_dir = Path(directory)
        if not scenario_dir.is_dir():
            raise FileNotFoundError(f"scenario directory not found: {scenario_dir}")

        scenarios: list[Scenario] = []
        for scenario_path in sorted(scenario_dir.glob("*.yaml")):
            scenarios.append(self.load_file(scenario_path))
        logger.info(f"loaded {len(scenarios)} scenario(s) from {scenario_dir}")
        return scenarios

    def _build_scenario(self, data: dict[str, Any]) -> Scenario:
        """Build a ``Scenario`` from the already-parsed YAML mapping.

        Args:
            data: The top-level mapping parsed from the YAML file.

        Returns:
            The validated ``Scenario``.
        """
        name = data["name"]
        reference_minutes = self._parse_clock(data.get("reference_time", "00:00"))

        route = self._build_route(data["route"])
        weights = self._build_weights(data.get("weights"))
        fleet_defaults = data.get("fleet_defaults", {})
        trips = self._build_trips(data["trips"], fleet_defaults, reference_minutes)

        scenario = Scenario(
            name=name,
            route=route,
            weights=weights,
            trips=trips,
            reference_minutes=reference_minutes,
        )
        logger.info(f"built scenario '{name}' with {len(trips)} trip(s)")
        return scenario

    def _build_route(self, route_data: dict[str, Any]) -> Route:
        """Build a ``Route`` (and its stations) from the YAML route mapping.

        Args:
            route_data: The ``route`` sub-mapping from the file.

        Returns:
            The validated ``Route``.
        """
        stops: list[RouteStop] = []
        for stop_data in route_data["stops"]:
            station = Station(
                name=stop_data["station"],
                chargers=stop_data.get("chargers", 1),
                charge_minutes=stop_data.get("charge_minutes", 25),
            )
            stops.append(RouteStop(station=station, position_km=stop_data["position_km"]))

        return Route(
            name=route_data["name"],
            origin=route_data["origin"],
            destination=route_data["destination"],
            total_length_km=route_data["total_length_km"],
            stops=tuple(stops),
        )

    def _build_weights(self, weights_data: dict[str, Any] | None) -> Weights:
        """Build a ``Weights`` from the YAML weights mapping (or use defaults).

        Args:
            weights_data: The ``weights`` sub-mapping, or ``None`` when omitted.

        Returns:
            A ``Weights`` carrying the file's values, or the model defaults when
            the file leaves weights unspecified.
        """
        if not weights_data:
            return Weights()

        values: dict[str, float] = {}
        for rule_key, weight in weights_data.items():
            values[rule_key] = float(weight)
        return Weights(values=values)

    def _build_trips(
        self,
        trips_data: list[dict[str, Any]],
        fleet_defaults: dict[str, Any],
        reference_minutes: int,
    ) -> tuple[Trip, ...]:
        """Build the tuple of ``Trip`` objects from the YAML trips list.

        Args:
            trips_data: The ``trips`` list from the file.
            fleet_defaults: Optional per-file bus physics defaults (range/speed).
            reference_minutes: The scenario's clock anchor in minutes-since-midnight,
                subtracted from each departure to get a relative ``departure_minute``.

        Returns:
            The validated trips, in file order.
        """
        default_range = fleet_defaults.get("range_km", 240)
        default_speed = fleet_defaults.get("speed_kmph", 60)

        trips: list[Trip] = []
        for trip_data in trips_data:
            bus = Bus(
                bus_id=trip_data["bus_id"],
                operator=Operator(trip_data["operator"]),
                range_km=trip_data.get("range_km", default_range),
                speed_kmph=trip_data.get("speed_kmph", default_speed),
            )
            departure_minute = self._parse_clock(trip_data["departure"]) - reference_minutes
            if departure_minute < 0:
                raise ValueError(f"trip {bus.bus_id} departs before the scenario reference time")
            trips.append(
                Trip(
                    bus=bus,
                    direction=Direction(trip_data["direction"]),
                    departure_minute=departure_minute,
                )
            )
        return tuple(trips)

    def _parse_clock(self, clock_text: str) -> int:
        """Convert an ``"HH:MM"`` clock string into minutes since midnight.

        Args:
            clock_text: A 24-hour clock time such as ``"19:15"``.

        Returns:
            The number of minutes since midnight (``19:15`` -> ``1155``).

        Raises:
            ValueError: If the text is not ``HH:MM`` with valid hour/minute.
        """
        parts = str(clock_text).split(":")
        if len(parts) != 2:
            raise ValueError(f"expected an 'HH:MM' time, got '{clock_text}'")

        hours = int(parts[0])
        minutes = int(parts[1])
        if not (0 <= hours < 24 and 0 <= minutes < 60):
            raise ValueError(f"'{clock_text}' is not a valid 24-hour time")
        return hours * 60 + minutes
