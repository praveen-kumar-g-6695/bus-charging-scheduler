"""
Scaling benchmark for the CP-SAT scheduler.

GOAL of this file (separate from main.py so we can experiment freely):
  Push CP-SAT from tiny to large and WATCH where it stops being instant.
  Later we run the SAME scenarios through greedy / local search and compare:
  does CP-SAT fall off a cliff while the heuristics stay fast?

WHY a special model here (and not main.py's tiny one):
  main.py STEP 9 was one station + one charger. That is EASY -- CP-SAT (or even a
  plain sort) solves it instantly no matter how many buses. The HARD part of the
  real problem is PLAN CHOICE: each bus may charge at SOME subset of stations, and
  those choices interact through the shared chargers. That choice is what makes the
  problem NP-hard, so THAT is what we must stress to find the scaling cliff.

The model we build per run:
  - `number_of_buses` buses, each with a departure minute.
  - `number_of_stations` stations equally spaced along a route.
  - `chargers_per_station` parallel chargers at each station.
  - Each bus MAY charge at each station -> a boolean "charges_here" decision.
  - A bus drives at 1 km/min; it may not travel more than `vehicle_range` km
    between two consecutive charges (or start->first, or last->end). This is the
    feasibility constraint and it forces buses to actually pick stations.
  - One charger holds one bus at a time (AddNoOverlap per charger lane).
  - Objective: minimise total wait across all buses.

We give the solver a TIME LIMIT so a hard instance cannot hang forever; if it hits
the limit without proving optimality we record that as "did not finish in time" --
which is itself the scaling signal we are hunting for.
"""

import time
from itertools import combinations

from ortools.sat.python import cp_model

CHARGE_MINUTES = 25


# ---------------------------------------------------------------------------
# 1) Make a synthetic world of a chosen size.
# ---------------------------------------------------------------------------
def build_synthetic_scenario(number_of_buses, number_of_stations,
                             route_length, vehicle_range, departure_gap):
    """Return (buses, station_positions) for a world of the requested size.

    buses           : list of {"name", "departure"} dicts.
    station_positions: list of km positions of each station along the route.
    """
    print(f"\n  building world: {number_of_buses} buses, "
          f"{number_of_stations} stations, route {route_length} km, "
          f"range {vehicle_range} km, departure gap {departure_gap} min")

    # Stations equally spaced strictly BETWEEN start (0) and end (route_length).
    station_positions = []
    spacing = route_length / (number_of_stations + 1)
    for station_index in range(1, number_of_stations + 1):
        station_positions.append(int(round(spacing * station_index)))
    print(f"  station positions (km): {station_positions}")

    # Buses depart one after another, departure_gap minutes apart.
    buses = []
    for bus_index in range(number_of_buses):
        bus_name = f"bus-{bus_index + 1:02d}"
        departure_minute = bus_index * departure_gap
        buses.append({"name": bus_name, "departure": departure_minute})

    return buses, station_positions


# ---------------------------------------------------------------------------
# 2) Build + solve the CP-SAT model for one world; return timing + result.
# ---------------------------------------------------------------------------
def solve_with_cpsat(buses, station_positions, route_length, vehicle_range,
                    chargers_per_station, time_limit_seconds):
    """Build the plan-choice model and solve it. Return a result dict."""
    number_of_buses = len(buses)
    number_of_stations = len(station_positions)

    build_start_wall = time.perf_counter()
    model = cp_model.CpModel()

    # A generous horizon: latest departure + driving the whole route + every bus
    # charging once at every station back to back. Always enough room.
    latest_departure = 0
    for bus in buses:
        if bus["departure"] > latest_departure:
            latest_departure = bus["departure"]
    horizon = (latest_departure + route_length
               + CHARGE_MINUTES * number_of_buses * number_of_stations)

    # For each (bus, station) we create:
    #   charges_here : bool, does this bus charge at this station?
    #   arrival      : the minute the bus REACHES this station (fixed: depart+km)
    #   start        : when charging starts (>= arrival)
    #   optional interval used by no-overlap ONLY when charges_here is true
    charges_here_by_key = {}
    start_by_key = {}
    wait_by_key = {}
    interval_by_key = {}

    # Collect intervals per station so we can forbid overlap on that station's
    # chargers.
    intervals_by_station_index = {}
    for station_index in range(number_of_stations):
        intervals_by_station_index[station_index] = []

    for bus in buses:
        bus_name = bus["name"]
        departure_minute = bus["departure"]

        # PRECEDENCE: walk this bus's stations in route order and accumulate the
        # time it loses at every earlier station it charges at. "consumed" at a
        # station = (wait there + 25 min charge) if it charges, else 0. The next
        # station's arrival is pushed back by all that accumulated lost time.
        consumed_before_this_station = []

        for station_index in range(number_of_stations):
            key = (bus_name, station_index)
            station_km = station_positions[station_index]

            charges_here = model.new_bool_var(f"charge_{bus_name}_{station_index}")

            # ARRIVAL is now a VARIABLE, not a constant: the minute this bus
            # physically reaches this km point = departure + distance driven +
            # every minute it already spent waiting & charging at earlier stops.
            arrival_var = model.new_int_var(0, horizon,
                                            f"arrival_{bus_name}_{station_index}")
            arrival_terms = [departure_minute + station_km]
            for earlier_consumed_var in consumed_before_this_station:
                arrival_terms.append(earlier_consumed_var)
            model.add(arrival_var == sum(arrival_terms))

            start_var = model.new_int_var(0, horizon,
                                          f"start_{bus_name}_{station_index}")
            end_var = model.new_int_var(0, horizon + CHARGE_MINUTES,
                                        f"end_{bus_name}_{station_index}")
            wait_var = model.new_int_var(0, horizon,
                                         f"wait_{bus_name}_{station_index}")

            # A bus can never start charging before it arrives.
            model.add(start_var >= arrival_var)

            # wait counts only if the bus actually charges here.
            model.add(wait_var == start_var - arrival_var).only_enforce_if(
                charges_here)
            model.add(wait_var == 0).only_enforce_if(charges_here.Not())
            # If it does not charge, pin start to arrival so nothing floats free.
            model.add(start_var == arrival_var).only_enforce_if(
                charges_here.Not())

            # consumed here feeds the NEXT station's arrival: wait + 25 if it
            # charges, otherwise it drives straight through losing nothing.
            consumed_var = model.new_int_var(0, horizon,
                                             f"consumed_{bus_name}_{station_index}")
            model.add(consumed_var == wait_var + CHARGE_MINUTES).only_enforce_if(
                charges_here)
            model.add(consumed_var == 0).only_enforce_if(charges_here.Not())

            # Optional interval: present in the no-overlap math only if it charges.
            optional_interval = model.new_optional_interval_var(
                start_var, CHARGE_MINUTES, end_var, charges_here,
                f"interval_{bus_name}_{station_index}")

            charges_here_by_key[key] = charges_here
            start_by_key[key] = start_var
            wait_by_key[key] = wait_var
            interval_by_key[key] = optional_interval
            intervals_by_station_index[station_index].append(optional_interval)

            # This station's consumed time delays every later station for this bus.
            consumed_before_this_station.append(consumed_var)

    # FEASIBILITY: between consecutive points the bus may not exceed its range.
    # Points = start(0) + chosen stations + end(route_length). Because plan choice
    # is dynamic, we enforce it as: for every pair of stations that are more than
    # `vehicle_range` apart with nothing in between chosen, the plan is illegal.
    # Simple sufficient encoding: any gap between two CONSECUTIVE chosen points
    # <= range. We approximate by requiring, for each window of stations whose
    # span from the previous mandatory reach exceeds range, at least one charge.
    add_range_feasibility(model, buses, station_positions, route_length,
                          vehicle_range, charges_here_by_key)

    # ONE charger per station originally; for N chargers we allow N overlapping
    # intervals by splitting into N no-overlap "lanes" is complex, so we use the
    # cumulative constraint: at any time the number of active intervals on a
    # station <= chargers_per_station.
    for station_index in range(number_of_stations):
        station_intervals = intervals_by_station_index[station_index]
        demands = []
        for one_interval in station_intervals:
            demands.append(1)
        model.add_cumulative(station_intervals, demands, chargers_per_station)

    # OBJECTIVE: minimise total wait across all (bus, station) charges.
    all_wait_vars = []
    for key in wait_by_key:
        all_wait_vars.append(wait_by_key[key])
    model.minimize(sum(all_wait_vars))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_seconds
    # Use several workers so the benchmark reflects real solver performance.
    solver.parameters.num_search_workers = 8

    build_seconds = time.perf_counter() - build_start_wall
    start_wall = time.perf_counter()
    status = solver.solve(model)
    elapsed_wall = time.perf_counter() - start_wall

    status_name = solver.status_name(status)
    proven_optimal = (status == cp_model.OPTIMAL)
    has_answer = status in (cp_model.OPTIMAL, cp_model.FEASIBLE)

    objective_value = None
    if has_answer:
        objective_value = solver.objective_value

    # Pull out the actual chosen schedule so we can PHYSICALLY validate it later.
    # For every (bus, station) record whether it charged and the start of charge.
    schedule_by_key = {}
    if has_answer:
        for key in charges_here_by_key:
            did_charge = solver.value(charges_here_by_key[key])
            charge_start = solver.value(start_by_key[key])
            charge_wait = solver.value(wait_by_key[key])
            schedule_by_key[key] = {
                "charges": did_charge,
                "start": charge_start,
                "wait": charge_wait,
            }

    result = {
        "buses": number_of_buses,
        "stations": number_of_stations,
        "chargers": chargers_per_station,
        "status": status_name,
        "proven_optimal": proven_optimal,
        "objective": objective_value,
        "build_seconds": build_seconds,
        "seconds": elapsed_wall,
        "schedule": schedule_by_key,
    }
    return result


def add_range_feasibility(model, buses, station_positions, route_length,
                          vehicle_range, charges_here_by_key):
    """Force each bus to charge often enough never to exceed its range.

    Encoding: list the points start(0), each station, end(route_length). For any
    two points whose distance is greater than the range, there must be at least
    one chosen charge strictly between them. The tightest such requirement is:
    walking from the start, the bus must charge before any reachable boundary is
    exceeded. We use the simple pairwise rule which is correct though not minimal.
    """
    number_of_stations = len(station_positions)

    for bus in buses:
        bus_name = bus["name"]

        # Build the ordered points with their "charge here" indicator.
        # start and end are fixed points that are always "reached" but never
        # charge; stations are optional charges.
        point_positions = [0]
        point_is_station = [False]
        point_station_index = [None]
        for station_index in range(number_of_stations):
            point_positions.append(station_positions[station_index])
            point_is_station.append(True)
            point_station_index.append(station_index)
        point_positions.append(route_length)
        point_is_station.append(False)
        point_station_index.append(None)

        # For every pair of points (earlier, later) that are farther apart than
        # the range, require at least one chosen charge among the stations that
        # lie STRICTLY BETWEEN them. The endpoints themselves must NOT count:
        # if we counted the endpoints, the pair (st0, st3) would be "covered" by
        # charging at st0 and st3 -- yet the gap st0->st3 could still exceed the
        # range. Strictly-between is what actually forces a refuel inside the gap.
        number_of_points = len(point_positions)
        for earlier_index in range(number_of_points):
            for later_index in range(earlier_index + 1, number_of_points):
                span = point_positions[later_index] - point_positions[earlier_index]
                if span > vehicle_range:
                    # Stations strictly between the two points (endpoints excluded).
                    covering_vars = []
                    for middle_index in range(earlier_index + 1, later_index):
                        if point_is_station[middle_index]:
                            station_index = point_station_index[middle_index]
                            key = (bus_name, station_index)
                            covering_vars.append(charges_here_by_key[key])
                    if len(covering_vars) > 0:
                        # At least one charge must happen inside this long span.
                        model.add(sum(covering_vars) >= 1)
                    else:
                        # No station lies inside an over-range gap: this pair can
                        # NEVER be bridged. If BOTH endpoints are chosen charges
                        # (or fixed start/end), the plan is physically impossible.
                        # Forbid charging at both endpoint stations simultaneously.
                        endpoint_charge_vars = []
                        if point_is_station[earlier_index]:
                            early_station = point_station_index[earlier_index]
                            endpoint_charge_vars.append(
                                charges_here_by_key[(bus_name, early_station)])
                        if point_is_station[later_index]:
                            late_station = point_station_index[later_index]
                            endpoint_charge_vars.append(
                                charges_here_by_key[(bus_name, late_station)])
                        if len(endpoint_charge_vars) == 2:
                            # Both can't be the consecutive stops across this gap.
                            model.add(sum(endpoint_charge_vars) <= 1)
                        elif len(endpoint_charge_vars) == 1:
                            # One side is a fixed start/end point we cannot avoid,
                            # and there is no station to bridge: the lone station
                            # endpoint cannot be the stop that crosses this gap.
                            # Geometry guarantees another charge is needed, but no
                            # candidate exists -> forbid this endpoint charge.
                            model.add(endpoint_charge_vars[0] == 0)


# ---------------------------------------------------------------------------
# 3) The SAME hard worlds, solved by our cheap heuristics for comparison.
#    These are standalone copies that work on the synthetic world (a list of
#    station positions), so they do NOT depend on main.py's fixed globals.
# ---------------------------------------------------------------------------
def feasible_plans_for_world(station_positions, route_length, vehicle_range):
    """Every range-valid set of stations (by INDEX) a bus could charge at."""
    number_of_stations = len(station_positions)
    station_indices = list(range(number_of_stations))

    valid_plans = []
    for number_of_charges in range(number_of_stations + 1):
        for chosen_indices in combinations(station_indices, number_of_charges):
            # Build the points the bus passes: start, chosen stations, end.
            points = [0]
            for station_index in chosen_indices:
                points.append(station_positions[station_index])
            points.append(route_length)

            plan_is_valid = True
            for point_index in range(len(points) - 1):
                gap = points[point_index + 1] - points[point_index]
                if gap > vehicle_range:
                    plan_is_valid = False
                    break

            if plan_is_valid:
                valid_plans.append(list(chosen_indices))
    return valid_plans


def evaluate_assignment_for_world(buses, assignment, station_positions,
                                  route_length, chargers_per_station):
    """Simulate every bus on its assigned plan; return total wait.

    `assignment` maps bus_name -> list of station indices that bus charges at.
    Each station has `chargers_per_station` chargers; we track when each lane
    becomes free and always pick the lane that frees up earliest.
    """
    number_of_stations = len(station_positions)

    # For each station keep a list of free-times, one per charger lane.
    free_times_by_station = {}
    for station_index in range(number_of_stations):
        lane_free_times = []
        for lane_number in range(chargers_per_station):
            lane_free_times.append(0)
        free_times_by_station[station_index] = lane_free_times

    buses_in_order = sorted(buses, key=lambda one_bus: one_bus["departure"])

    total_wait = 0
    for bus in buses_in_order:
        bus_name = bus["name"]
        chosen_plan = assignment[bus_name]

        current_time = bus["departure"]
        current_position = 0

        for station_index in chosen_plan:
            station_km = station_positions[station_index]
            distance_to_drive = station_km - current_position
            arrival_time = current_time + distance_to_drive

            # Pick the charger lane that is free earliest at this station.
            lane_free_times = free_times_by_station[station_index]
            earliest_lane_number = 0
            earliest_free_time = lane_free_times[0]
            for lane_number in range(len(lane_free_times)):
                if lane_free_times[lane_number] < earliest_free_time:
                    earliest_free_time = lane_free_times[lane_number]
                    earliest_lane_number = lane_number

            if arrival_time >= earliest_free_time:
                charge_start = arrival_time
                wait_minutes = 0
            else:
                charge_start = earliest_free_time
                wait_minutes = earliest_free_time - arrival_time

            charge_end = charge_start + CHARGE_MINUTES
            lane_free_times[earliest_lane_number] = charge_end

            total_wait = total_wait + wait_minutes
            current_time = charge_end
            current_position = station_km

    return total_wait


def solve_with_greedy(buses, station_positions, route_length, vehicle_range,
                      chargers_per_station):
    """Each bus, in departure order, picks the plan giving it the least wait."""
    candidate_plans = feasible_plans_for_world(
        station_positions, route_length, vehicle_range
    )
    number_of_stations = len(station_positions)

    free_times_by_station = {}
    for station_index in range(number_of_stations):
        lane_free_times = []
        for lane_number in range(chargers_per_station):
            lane_free_times.append(0)
        free_times_by_station[station_index] = lane_free_times

    buses_in_order = sorted(buses, key=lambda one_bus: one_bus["departure"])

    assignment = {}
    start_wall = time.perf_counter()

    for bus in buses_in_order:
        bus_name = bus["name"]
        departure_minute = bus["departure"]

        best_plan = None
        best_plan_wait = None

        # Try each feasible plan against the CURRENT charger state (look only).
        for candidate_plan in candidate_plans:
            current_time = departure_minute
            current_position = 0
            plan_wait = 0

            for station_index in candidate_plan:
                station_km = station_positions[station_index]
                arrival_time = current_time + (station_km - current_position)

                lane_free_times = free_times_by_station[station_index]
                earliest_free_time = lane_free_times[0]
                for lane_number in range(len(lane_free_times)):
                    if lane_free_times[lane_number] < earliest_free_time:
                        earliest_free_time = lane_free_times[lane_number]

                if arrival_time >= earliest_free_time:
                    charge_start = arrival_time
                else:
                    charge_start = earliest_free_time
                plan_wait = plan_wait + (charge_start - arrival_time)
                current_time = charge_start + CHARGE_MINUTES
                current_position = station_km

            if best_plan_wait is None or plan_wait < best_plan_wait:
                best_plan_wait = plan_wait
                best_plan = candidate_plan

        # COMMIT the chosen plan: block the chargers it uses.
        assignment[bus_name] = best_plan
        current_time = departure_minute
        current_position = 0
        for station_index in best_plan:
            station_km = station_positions[station_index]
            arrival_time = current_time + (station_km - current_position)

            lane_free_times = free_times_by_station[station_index]
            earliest_lane_number = 0
            earliest_free_time = lane_free_times[0]
            for lane_number in range(len(lane_free_times)):
                if lane_free_times[lane_number] < earliest_free_time:
                    earliest_free_time = lane_free_times[lane_number]
                    earliest_lane_number = lane_number

            if arrival_time >= earliest_free_time:
                charge_start = arrival_time
            else:
                charge_start = earliest_free_time
            lane_free_times[earliest_lane_number] = charge_start + CHARGE_MINUTES
            current_time = charge_start + CHARGE_MINUTES
            current_position = station_km

    total_wait = evaluate_assignment_for_world(
        buses, assignment, station_positions, route_length, chargers_per_station
    )
    elapsed_wall = time.perf_counter() - start_wall

    return {"assignment": assignment, "total_wait": total_wait,
            "seconds": elapsed_wall}


def solve_with_local_search(buses, station_positions, route_length,
                            vehicle_range, chargers_per_station,
                            time_budget_seconds):
    """Start from greedy, then take the best 1-swap until no swap improves.

    A time budget stops the search gracefully on very large instances (where a
    single pass over buses*plans neighbors is itself expensive).
    """
    candidate_plans = feasible_plans_for_world(
        station_positions, route_length, vehicle_range
    )

    start_wall = time.perf_counter()
    greedy_result = solve_with_greedy(
        buses, station_positions, route_length, vehicle_range,
        chargers_per_station
    )
    current_assignment = dict(greedy_result["assignment"])
    current_wait = evaluate_assignment_for_world(
        buses, current_assignment, station_positions, route_length,
        chargers_per_station
    )

    hit_time_budget = False
    while True:
        best_move = None
        best_move_wait = current_wait

        for bus in buses:
            # Stop if we have spent our time budget mid-pass.
            if time.perf_counter() - start_wall > time_budget_seconds:
                hit_time_budget = True
                break

            bus_name = bus["name"]
            current_plan = current_assignment[bus_name]
            for candidate_plan in candidate_plans:
                if candidate_plan != current_plan:
                    neighbor_assignment = dict(current_assignment)
                    neighbor_assignment[bus_name] = candidate_plan
                    neighbor_wait = evaluate_assignment_for_world(
                        buses, neighbor_assignment, station_positions,
                        route_length, chargers_per_station
                    )
                    if neighbor_wait < best_move_wait:
                        best_move_wait = neighbor_wait
                        best_move = (bus_name, candidate_plan)

        if hit_time_budget or best_move is None:
            break

        bus_to_change = best_move[0]
        new_plan = best_move[1]
        current_assignment[bus_to_change] = new_plan
        current_wait = best_move_wait

    elapsed_wall = time.perf_counter() - start_wall
    return {"assignment": current_assignment, "total_wait": current_wait,
            "seconds": elapsed_wall, "hit_time_budget": hit_time_budget}


# ---------------------------------------------------------------------------
# 4) The sweep: run increasing sizes through ALL THREE engines, print + save.
# ---------------------------------------------------------------------------
def run_scaling_sweep():
    """Solve a ladder of growing problems with CP-SAT, greedy, local search."""
    print("\n=================== SCALING SWEEP (3 ENGINES) ===================")

    route_length = 540
    vehicle_range = 240
    departure_gap = 8          # tight spacing => heavy charger contention
    chargers_per_station = 1
    time_limit_seconds = 20.0  # cap so a hard CP-SAT instance cannot hang

    sizes_to_try = [
        (3, 4),
        (6, 4),
        (10, 4),
        (20, 4),
        (40, 4),
        (20, 6),
        (40, 6),
        (60, 6),
        (80, 8),
        (120, 8),
        # Large-scale stress: 1000 buses, varying the number of stations so we
        # can watch how station count (slack) changes CP-SAT's behaviour under
        # the 20s cap, and whether the heuristics still answer quickly.
        (1000, 4),
        (1000, 6),
        (1000, 8),
        (1000, 12),
    ]

    # Collect human-readable report lines; we print them AND write them to file.
    report_lines = []
    report_lines.append("CP-SAT vs GREEDY vs LOCAL SEARCH -- scaling comparison")
    report_lines.append(f"route_length={route_length} km, range={vehicle_range} "
                        f"km, departure_gap={departure_gap} min, "
                        f"chargers/station={chargers_per_station}, "
                        f"cpsat_time_limit={time_limit_seconds}s")
    report_lines.append("")
    header = ("buses  stat  | cpsat_status  cpsat_opt  cpsat_obj  cpsat_build_s "
              "cpsat_solve_s | greedy_obj greedy_s | local_obj local_s")
    report_lines.append(header)
    report_lines.append("-" * len(header))

    for size in sizes_to_try:
        number_of_buses = size[0]
        number_of_stations = size[1]

        buses, station_positions = build_synthetic_scenario(
            number_of_buses, number_of_stations, route_length,
            vehicle_range, departure_gap
        )

        cpsat_result = solve_with_cpsat(
            buses, station_positions, route_length, vehicle_range,
            chargers_per_station, time_limit_seconds
        )
        greedy_result = solve_with_greedy(
            buses, station_positions, route_length, vehicle_range,
            chargers_per_station
        )
        local_result = solve_with_local_search(
            buses, station_positions, route_length, vehicle_range,
            chargers_per_station, time_limit_seconds
        )

        cpsat_objective_text = "n/a"
        if cpsat_result["objective"] is not None:
            cpsat_objective_text = f"{cpsat_result['objective']:.0f}"

        line = (f"{number_of_buses:>5}  {number_of_stations:>4}  | "
                f"{cpsat_result['status']:<13} "
                f"{str(cpsat_result['proven_optimal']):<9} "
                f"{cpsat_objective_text:>9} "
                f"{cpsat_result['build_seconds']:>13.3f} "
                f"{cpsat_result['seconds']:>13.3f} | "
                f"{greedy_result['total_wait']:>10.0f} "
                f"{greedy_result['seconds']:>8.4f} | "
                f"{local_result['total_wait']:>9.0f} "
                f"{local_result['seconds']:>7.4f}")
        report_lines.append(line)
        print(line)

    report_lines.append("")
    report_lines.append("Reading this table:")
    report_lines.append("- cpsat_opt False = solver hit the time limit without "
                        "PROVING optimal (its obj is best-found, maybe not best).")
    report_lines.append("- Compare greedy_obj / local_obj against cpsat_obj: how "
                        "close do the cheap heuristics get to the optimum?")
    report_lines.append("- Compare the *_s columns: greedy/local stay in "
                        "milliseconds while cpsat_s climbs / hits the limit.")

    # Write the report next to this script so it can be opened and read.
    report_text = "\n".join(report_lines)
    output_path = "benchmark_results.txt"
    output_file = open(output_path, "w", encoding="utf-8")
    output_file.write(report_text)
    output_file.write("\n")
    output_file.close()

    print(f"\n  results written to {output_path}")
    return report_lines


if __name__ == "__main__":
    run_scaling_sweep()
