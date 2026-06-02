"""The Bus Charging Scheduler web app (Streamlit).

Run it from the project root (``usb_charg/``) with::

    uv run streamlit run app/ui/streamlit_app.py

WHAT THE REVIEWER SEES
----------------------
One dropdown to pick a SCENARIO. Everything else is read-only output for that
scenario:

1. SCENARIO INPUT -- the raw situation as data: the route and its stations, the
   tunable weights, and the timetable of trips.
2. PER-BUS TIMETABLE -- for every bus: which stations it charges at, when it
   arrives, how long it waits, and when it finally reaches its destination.
3. PER-STATION VIEW -- for every charger: the order buses use it.

Per the assignment, there is deliberately NO strategy picker and no metrics
dashboard. The scheduling algorithm is chosen internally by the factory (the
benchmark-backed default, a local search seeded by the global greedy); this file
only presents its result. All scheduling/simulation/scoring lives in ``app`` --
this module is pure presentation.
"""

import sys
from pathlib import Path

# ``streamlit run`` puts THIS file's directory (app/ui) on sys.path, not the
# project root, so ``import app`` would fail. Put the project root (the parent of
# the ``app`` package) on the path before importing anything from ``app``.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import streamlit as st  # noqa: E402  (must follow the sys.path bootstrap above)

from app.domain.models import Scenario, Trip  # noqa: E402
from app.domain.results import ScheduleResult, TripTimeline  # noqa: E402
from app.io.loader import ScenarioLoader  # noqa: E402
from app.logging_config import get_logger, setup_logging  # noqa: E402
from app.scheduling.factory import create_scheduler  # noqa: E402

_SCENARIOS_DIR = _PROJECT_ROOT / "scenarios"

# setup_logging is idempotent, so calling it on every Streamlit rerun is safe; it
# attaches handlers only once. INFO keeps the server console readable (the heavy
# DEBUG trace is only useful from the CLI).
setup_logging(level="INFO")
logger = get_logger(__name__)


def _format_minutes(minutes: int, reference_minutes: int) -> str:
    """Render a relative minute count as a real ``HH:MM`` wall-clock time.

    All times in the domain are minutes since each scenario's own start; the
    scenario carries the clock anchor (e.g. 19:00) so we can add the two and show
    the actual departure/arrival time a dispatcher would read. Times that spill
    past midnight wrap around the 24-hour clock.

    Args:
        minutes: Minutes since the scenario start.
        reference_minutes: The scenario's clock anchor in minutes-since-midnight.

    Returns:
        The wall-clock time formatted as ``"HH:MM"``.
    """
    clock_minutes = (reference_minutes + minutes) % (24 * 60)
    hours, remainder = divmod(clock_minutes, 60)
    return f"{hours:02d}:{remainder:02d}"


def _direction_label(scenario: Scenario, trip: Trip) -> str:
    """Describe a trip's direction using the route's real endpoint cities.

    Args:
        scenario: The scenario whose route names the endpoints.
        trip: The trip whose direction to describe.

    Returns:
        A human-readable ``"Origin -> Destination"`` label.
    """
    route = scenario.route
    sequence = route.station_sequence(trip.direction)
    if not sequence:
        return trip.direction.value
    # The first station in travel order tells us which end the trip starts from;
    # simpler and always correct: forward goes origin->destination, reverse the
    # other way.
    if trip.direction.value == "forward":
        return f"{route.origin} -> {route.destination}"
    return f"{route.destination} -> {route.origin}"


@st.cache_resource
def _load_scenarios() -> list[Scenario]:
    """Load every scenario file once and cache it for the server's lifetime.

    The scenario files are static, so loading them is cached with
    ``st.cache_resource``; the (fast) scheduling itself is deliberately NOT cached
    so picking a scenario always re-runs the live engine.

    Returns:
        Every scenario in ``scenarios/``, sorted by file name.
    """
    logger.info(f"loading scenarios from {_SCENARIOS_DIR}")
    return ScenarioLoader().load_dir(_SCENARIOS_DIR)


def _render_scenario_input(scenario: Scenario) -> None:
    """Render the raw scenario data: route, weights and the trip timetable.

    Args:
        scenario: The selected scenario to display.

    Returns:
        None.
    """
    st.subheader("Scenario input")

    route = scenario.route
    st.markdown(
        f"**Route:** {route.name} &nbsp;|&nbsp; {route.origin} -> "
        f"{route.destination} &nbsp;|&nbsp; {route.total_length_km} km total"
    )

    station_rows = []
    for stop in route.stops:
        station_rows.append(
            {
                "Station": stop.station.name,
                "Distance from origin (km)": stop.position_km,
                "Chargers (lanes)": stop.station.chargers,
                "Charge time (min)": stop.station.charge_minutes,
            }
        )
    st.markdown("**Charging stations**")
    st.dataframe(station_rows, hide_index=True, width="stretch")

    weight_rows = []
    for rule_key, weight_value in scenario.weights.values.items():
        weight_rows.append({"Objective (rule)": rule_key, "Weight": weight_value})
    st.markdown("**Objective weights** (how much each goal matters)")
    st.dataframe(weight_rows, hide_index=True, width="stretch")

    trip_rows = []
    for trip in scenario.trips:
        trip_rows.append(
            {
                "Bus": trip.bus.bus_id,
                "Operator": trip.bus.operator.value,
                "Direction": _direction_label(scenario, trip),
                "Departs": _format_minutes(trip.departure_minute, scenario.reference_minutes),
                "Range (km)": trip.bus.range_km,
                "Speed (km/h)": trip.bus.speed_kmph,
            }
        )
    st.markdown(f"**Trips** ({len(scenario.trips)} buses)")
    st.dataframe(trip_rows, hide_index=True, width="stretch")


def _render_bus_timetable(scenario: Scenario, timeline: TripTimeline) -> None:
    """Render one bus's journey: its charges, waits and final arrival.

    Args:
        scenario: The scenario being displayed (for the direction label).
        timeline: The trip timeline to render.

    Returns:
        None.
    """
    trip = timeline.trip
    bus = trip.bus
    header = (
        f"{bus.bus_id} &nbsp;|&nbsp; {bus.operator.value} &nbsp;|&nbsp; "
        f"{_direction_label(scenario, trip)}"
    )
    st.markdown(f"**{header}**")

    if not timeline.charge_events:
        st.caption("Drives straight through -- no charging needed.")
    else:
        reference_minutes = scenario.reference_minutes
        event_rows = []
        for charge_index, event in enumerate(timeline.charge_events, start=1):
            event_rows.append(
                {
                    "Stop #": charge_index,
                    "Station": event.station.name,
                    "Arrives": _format_minutes(event.arrival_minute, reference_minutes),
                    "Wait (min)": event.wait_minutes,
                    "Charge starts": _format_minutes(event.start_minute, reference_minutes),
                    "Charge ends": _format_minutes(event.end_minute, reference_minutes),
                }
            )
        st.dataframe(event_rows, hide_index=True, width="stretch")

    st.caption(
        f"Arrives destination at "
        f"{_format_minutes(timeline.arrival_minute, scenario.reference_minutes)} "
        f"&nbsp;|&nbsp; total wait {timeline.total_wait_minutes} min"
    )


def _render_station_view(result: ScheduleResult) -> None:
    """Render, per station, the order in which buses charge there.

    Args:
        result: The scheduled result to read station schedules from.

    Returns:
        None.
    """
    st.subheader("Per-station view -- order buses charge")

    reference_minutes = result.scenario.reference_minutes
    station_schedules = result.station_schedules()
    if not station_schedules:
        st.caption("No bus charges anywhere in this scenario.")
        return

    for station_schedule in station_schedules:
        st.markdown(f"**Station {station_schedule.station.name}**")
        order_rows = []
        for order_index, event in enumerate(station_schedule.events, start=1):
            order_rows.append(
                {
                    "Order": order_index,
                    "Bus": event.trip.bus.bus_id,
                    "Operator": event.trip.bus.operator.value,
                    "Arrives": _format_minutes(event.arrival_minute, reference_minutes),
                    "Wait (min)": event.wait_minutes,
                    "Charge starts": _format_minutes(event.start_minute, reference_minutes),
                    "Charge ends": _format_minutes(event.end_minute, reference_minutes),
                }
            )
        st.dataframe(order_rows, hide_index=True, width="stretch")


def main() -> None:
    """Lay out the page: pick a scenario, schedule it, and show the result.

    Returns:
        None.
    """
    st.set_page_config(page_title="Bus Charging Scheduler", layout="wide")
    st.title("Bus Charging Scheduler")
    st.caption(
        "Electric buses charging at shared stations along the Bengaluru <-> Kochi "
        "route. Pick a scenario to see the computed schedule."
    )

    scenarios = _load_scenarios()
    if not scenarios:
        st.error(f"No scenario files found in {_SCENARIOS_DIR}.")
        return

    scenario_by_name = {scenario.name: scenario for scenario in scenarios}
    selected_name = st.selectbox("Scenario", list(scenario_by_name.keys()))
    scenario = scenario_by_name[selected_name]
    logger.info(f"user selected scenario '{scenario.name}'")

    _render_scenario_input(scenario)

    # Schedule with the factory default (local search seeded by the global
    # greedy). Fast enough (<1s) to run live on every selection, so no caching.
    scheduler = create_scheduler()
    result = scheduler.schedule(scenario)

    st.divider()
    st.subheader("Per-bus timetable")
    for timeline in result.trip_timelines:
        _render_bus_timetable(scenario, timeline)

    st.divider()
    _render_station_view(result)


main()
