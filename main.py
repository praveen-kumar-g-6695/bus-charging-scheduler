"""
Bus Charging Scheduler — built step by step.

We use the REAL constraints from the assignment:
  Route (Bengaluru -> Kochi):  BLR --100-- A --120-- B --100-- C --120-- D --100-- KOCHI
  Total = 540 km
  Battery range = 240 km on a full charge
  Charging = 25 min, always to full
  Speed = 60 km/h  =>  travel minutes == kilometres  (our assumption, keeps math clear)

STEP 1: just one function -> enumerate the FEASIBLE charging plans for ONE bus.
"""

from itertools import combinations

from ortools.sat.python import cp_model

# ---------------------------------------------------------------------------
# The world, as plain data (later this moves into a scenario file)
# ---------------------------------------------------------------------------
# Station -> distance in km from Bengaluru (the start, position 0).
STATIONS = {"A": 100, "B": 220, "C": 320, "D": 440}
ROUTE_LENGTH = 540    # km, Bengaluru -> Kochi
RANGE = 240           # km a bus can travel on one full charge
CHARGE_MINUTES = 25   # how long a charge takes (always to full)

# The tunable knobs. ONE obvious place. Bigger number = we care more about
# that thing when picking a plan. Later these come from the scenario file.
WEIGHTS = {
    "individual_wait": 1.0,    # how much this bus dislikes waiting at chargers
    "individual_arrival": 1.0,  # how much this bus dislikes arriving late
    "overall": 1.0,            # how much we care about the whole-network arrival
}


# ---------------------------------------------------------------------------
# STEP 1: feasible plans for ONE bus
# ---------------------------------------------------------------------------
def feasible_plans():
    """Return every set of charging stations a bus COULD use, where the bus
    never travels more than RANGE km between two consecutive charges.

    A 'plan' = the list of stations (in route order) where the bus charges.
    """
    station_names = list(STATIONS.keys())   # ["A", "B", "C", "D"] -- already in route order

    station_positions = []
    for station_name in station_names:
        station_positions.append(STATIONS[station_name])

    print(f"Stations in route order: {station_names}")
    print(f"Their positions (km from start): {station_positions}")
    print(f"Route length: {ROUTE_LENGTH} km, Range: {RANGE} km\n")

    valid_plans = []

    # Try every possible NUMBER of charges the bus could make: 0, 1, 2, 3, 4.
    for number_of_charges in range(len(station_names) + 1):
        print(f"=== Trying plans with {number_of_charges} charge(s) ===")

        # combinations(station_names, number_of_charges) hands us every way to
        # pick exactly that many stations, WITHOUT repeating and keeping route
        # order. Example for 2 charges out of A,B,C,D it yields, one at a time:
        #   ('A','B'), ('A','C'), ('A','D'), ('B','C'), ('B','D'), ('C','D')
        # Each of those tuples is ONE candidate plan we are about to test.
        for chosen_stations in combinations(station_names, number_of_charges):
            print(f"  Candidate plan -> charge at: {list(chosen_stations)}")

            # Turn the chosen station names into their km positions.
            chosen_positions = []
            for station_name in chosen_stations:
                chosen_positions.append(STATIONS[station_name])
            print(f"      station positions (km): {chosen_positions}")

            # Build the full list of points the bus passes through:
            # start (0)  ->  each charging station  ->  end (ROUTE_LENGTH).
            points = [0]
            for position in chosen_positions:
                points.append(position)
            points.append(ROUTE_LENGTH)
            print(f"      full points incl. start/end: {points}")

            # Measure how far the bus drives between each pair of points.
            gaps = []
            for index in range(len(points) - 1):
                distance_between = points[index + 1] - points[index]
                gaps.append(distance_between)
            print(f"      gaps between points (km): {gaps}")

            # The plan is valid ONLY if no single gap exceeds the battery range.
            plan_is_valid = True
            for gap in gaps:
                if gap > RANGE:
                    plan_is_valid = False
                    print(f"      -> gap {gap} km > range {RANGE} km  =>  INVALID")
                    break

            if plan_is_valid:
                print("      -> all gaps within range  =>  VALID")
                valid_plans.append(list(chosen_stations))

        print()

    print(f"==> {len(valid_plans)} feasible plan(s): {valid_plans}")
    return valid_plans


# ---------------------------------------------------------------------------
# STEP 2: walk ONE bus down ONE chosen plan and build its timeline.
# No other buses yet -> NO waiting for a charger. Just travel + charge math.
# Speed is 60 km/h, so driving X km takes X minutes.
# ---------------------------------------------------------------------------
def build_timeline(bus_name, departure_minute, chosen_plan):
    """Follow one bus from the start to Kochi using one charging plan.

    Returns a list of 'events', one per charging stop, plus we print the
    final arrival time.
    """
    print(f"\n--- Building timeline for {bus_name} ---")
    print(f"Departs at minute {departure_minute}, plan = charge at {chosen_plan}")

    current_time = departure_minute   # the clock, in minutes
    current_position = 0              # km from the start (Bengaluru)
    events = []

    # Drive to each charging station in the plan, one at a time.
    for station_name in chosen_plan:
        station_position = STATIONS[station_name]

        # How far to drive from where we are to this station, and how long.
        distance_to_drive = station_position - current_position
        travel_minutes = distance_to_drive   # 60 km/h => km == minutes
        arrival_time = current_time + travel_minutes
        print(f"  Drive {distance_to_drive} km to {station_name}: "
              f"leave at {current_time}, arrive at {arrival_time}")

        # With no other buses, the bus charges the moment it arrives.
        charge_start = arrival_time
        charge_end = charge_start + CHARGE_MINUTES
        print(f"  Charge at {station_name}: {charge_start} -> {charge_end} "
              f"({CHARGE_MINUTES} min)")

        events.append({
            "station": station_name,
            "arrival": arrival_time,
            "charge_start": charge_start,
            "charge_end": charge_end,
        })

        # Move the clock and the bus forward to after this charge.
        current_time = charge_end
        current_position = station_position

    # Drive the final leg from the last station to Kochi (the end).
    distance_to_end = ROUTE_LENGTH - current_position
    final_arrival = current_time + distance_to_end
    print(f"  Drive final {distance_to_end} km to Kochi: "
          f"leave at {current_time}, ARRIVE at {final_arrival}")

    return events, final_arrival


# ---------------------------------------------------------------------------
# STEP 3: run SEVERAL buses on the SAME plan, sharing ONE charger per station.
# New idea: each station's charger can serve only one bus at a time. We track
# "when is this charger next free". If a bus arrives before the charger is
# free, it has to WAIT.
# ---------------------------------------------------------------------------
def schedule_buses(buses, shared_plan):
    """Run every bus through the same plan, but respect one charger per station.

    `buses` is a list of dicts like {"name": "bus-BK-01", "departure": 0}.
    We process buses in departure order so the earlier bus claims the charger
    first.
    """
    print("\n========== SCHEDULING ALL BUSES ==========")
    print(f"Everyone uses plan: charge at {shared_plan}\n")

    # For each station, remember the minute its charger becomes free again.
    # At the start (minute 0) every charger is free.
    charger_free_at = {}
    for station_name in STATIONS:
        charger_free_at[station_name] = 0
    print(f"Charger free-at times to begin with: {charger_free_at}\n")

    # Sort the buses so the one leaving earliest is handled first.
    buses_in_order = sorted(buses, key=lambda one_bus: one_bus["departure"])

    all_timelines = []

    for bus in buses_in_order:
        bus_name = bus["name"]
        print(f"--- {bus_name} (departs {bus['departure']}) ---")

        current_time = bus["departure"]
        current_position = 0
        events = []

        for station_name in shared_plan:
            station_position = STATIONS[station_name]

            # Drive to the station.
            distance_to_drive = station_position - current_position
            arrival_time = current_time + distance_to_drive
            print(f"  Arrive {station_name} at minute {arrival_time}")

            # Is the charger free yet? If not, wait until it is.
            free_time = charger_free_at[station_name]
            print(f"    charger at {station_name} is free at {free_time}")
            if arrival_time >= free_time:
                charge_start = arrival_time
                wait_minutes = 0
                print("    charger is free -> start immediately")
            else:
                charge_start = free_time
                wait_minutes = free_time - arrival_time
                print(f"    charger BUSY -> wait {wait_minutes} min, "
                      f"start at {charge_start}")

            charge_end = charge_start + CHARGE_MINUTES
            print(f"    charge {charge_start} -> {charge_end}")

            # This charger is now blocked until charge_end for the next bus.
            charger_free_at[station_name] = charge_end

            events.append({
                "station": station_name,
                "arrival": arrival_time,
                "wait": wait_minutes,
                "charge_start": charge_start,
                "charge_end": charge_end,
            })

            current_time = charge_end
            current_position = station_position

        # Final drive to Kochi.
        distance_to_end = ROUTE_LENGTH - current_position
        final_arrival = current_time + distance_to_end
        print(f"  ARRIVE Kochi at minute {final_arrival}")
        print(f"  charger free-at now: {charger_free_at}\n")

        all_timelines.append({
            "name": bus_name,
            "events": events,
            "final_arrival": final_arrival,
        })

    return all_timelines


# ---------------------------------------------------------------------------
# STEP 4: let ONE bus pick its OWN best plan.
# Until now every bus used the same plan we handed it. Now the bus looks at
# ALL its feasible plans, mentally simulates each one against the chargers'
# current free-times, and keeps the plan that gets it to Kochi the EARLIEST.
# This only LOOKS -- it does not block any charger. Choosing != committing.
# ---------------------------------------------------------------------------
def choose_best_plan(bus_name, departure_minute, candidate_plans, charger_free_at):
    """Return the plan (from candidate_plans) with the earliest arrival.

    `charger_free_at` is the CURRENT free-time per station. We do NOT change it
    here -- we only read it to predict waits.
    """
    print(f"\n===== {bus_name} is choosing a plan (departs {departure_minute}) =====")
    print(f"Charger free-at right now: {charger_free_at}")

    best_plan = None
    best_arrival = None

    for candidate_plan in candidate_plans:
        print(f"\n  Trying plan {candidate_plan}:")

        current_time = departure_minute
        current_position = 0
        total_wait = 0

        for station_name in candidate_plan:
            station_position = STATIONS[station_name]

            distance_to_drive = station_position - current_position
            arrival_time = current_time + distance_to_drive

            free_time = charger_free_at[station_name]
            if arrival_time >= free_time:
                charge_start = arrival_time
                wait_minutes = 0
            else:
                charge_start = free_time
                wait_minutes = free_time - arrival_time

            total_wait = total_wait + wait_minutes
            charge_end = charge_start + CHARGE_MINUTES
            print(f"    {station_name}: arrive {arrival_time}, "
                  f"wait {wait_minutes}, charge {charge_start}->{charge_end}")

            current_time = charge_end
            current_position = station_position

        distance_to_end = ROUTE_LENGTH - current_position
        predicted_arrival = current_time + distance_to_end
        print(f"    => predicted arrival {predicted_arrival}, "
              f"total wait {total_wait}")

        # Keep this plan if it's the first one or beats the best so far.
        if best_arrival is None or predicted_arrival < best_arrival:
            best_arrival = predicted_arrival
            best_plan = candidate_plan
            print(f"    *** new best plan so far: {best_plan} "
                  f"(arrival {best_arrival}) ***")

    print(f"\n  CHOSEN plan for {bus_name}: {best_plan} (arrival {best_arrival})")
    return best_plan


# ---------------------------------------------------------------------------
# STEP 5: the FULL greedy scheduler.
# We process buses in departure order. For EACH bus we:
#   1) choose its best plan against the CURRENT charger free-times  (step 4)
#   2) actually run that plan and BLOCK the chargers it uses         (step 3)
# Because step 2 updates charger_free_at, the NEXT bus sees the new reality.
# That feedback between buses is what makes this a real schedule.
# ---------------------------------------------------------------------------
def run_scheduler(buses, candidate_plans):
    """Greedily schedule every bus, committing each bus before the next."""
    print("\n############## RUNNING THE FULL SCHEDULER ##############")

    # Start with every charger free at minute 0.
    charger_free_at = {}
    for station_name in STATIONS:
        charger_free_at[station_name] = 0

    # Earliest-departing bus goes first.
    buses_in_order = sorted(buses, key=lambda one_bus: one_bus["departure"])

    all_timelines = []

    for bus in buses_in_order:
        bus_name = bus["name"]
        departure_minute = bus["departure"]

        # --- 1) CHOOSE: pick the best plan given the chargers' current state.
        chosen_plan = choose_best_plan(
            bus_name, departure_minute, candidate_plans, charger_free_at
        )

        # --- 2) COMMIT: actually run that plan and block the chargers it uses.
        print(f"\n  >>> COMMITTING {bus_name} onto plan {chosen_plan}")
        current_time = departure_minute
        current_position = 0
        events = []

        for station_name in chosen_plan:
            station_position = STATIONS[station_name]

            distance_to_drive = station_position - current_position
            arrival_time = current_time + distance_to_drive

            free_time = charger_free_at[station_name]
            if arrival_time >= free_time:
                charge_start = arrival_time
                wait_minutes = 0
            else:
                charge_start = free_time
                wait_minutes = free_time - arrival_time

            charge_end = charge_start + CHARGE_MINUTES

            # THIS is the commit: the charger is now blocked for later buses.
            charger_free_at[station_name] = charge_end
            print(f"      {station_name}: arrive {arrival_time}, "
                  f"wait {wait_minutes}, charge {charge_start}->{charge_end} "
                  f"(charger now free at {charge_end})")

            events.append({
                "station": station_name,
                "arrival": arrival_time,
                "wait": wait_minutes,
                "charge_start": charge_start,
                "charge_end": charge_end,
            })

            current_time = charge_end
            current_position = station_position

        distance_to_end = ROUTE_LENGTH - current_position
        final_arrival = current_time + distance_to_end
        print(f"      ARRIVE Kochi at {final_arrival}")
        print(f"      charger state after {bus_name}: {charger_free_at}")

        all_timelines.append({
            "name": bus_name,
            "plan": chosen_plan,
            "events": events,
            "final_arrival": final_arrival,
        })

    return all_timelines


# ---------------------------------------------------------------------------
# STEP 6: score a plan with WEIGHTS instead of just "earliest arrival".
# Until now a bus picked whichever plan arrived earliest. That is only ONE
# thing to care about. Now we measure several things about a plan, multiply
# each by its weight, and add them up into a single COST. Lower cost = better.
# Changing a weight (top of file) changes which plan wins -> tunable behaviour.
# ---------------------------------------------------------------------------
def score_plan(total_wait, predicted_arrival):
    """Turn the facts about a simulated plan into one cost number to minimise.

    cost =  weight_wait    * total_wait
          + weight_arrival * predicted_arrival
          + weight_overall * predicted_arrival   (its share of network time)
    """
    wait_part = WEIGHTS["individual_wait"] * total_wait
    arrival_part = WEIGHTS["individual_arrival"] * predicted_arrival
    overall_part = WEIGHTS["overall"] * predicted_arrival

    cost = wait_part + arrival_part + overall_part
    print(f"      score: wait_part={wait_part} "
          f"(w={WEIGHTS['individual_wait']} * wait={total_wait}) + "
          f"arrival_part={arrival_part} + overall_part={overall_part} "
          f"=> COST={cost}")
    return cost


def choose_best_plan_weighted(bus_name, departure_minute,
                              candidate_plans, charger_free_at):
    """Like choose_best_plan, but picks the LOWEST-COST plan (via score_plan),
    not just the earliest arrival.
    """
    print(f"\n===== {bus_name} choosing by WEIGHTED score (departs "
          f"{departure_minute}) =====")
    print(f"Weights in use: {WEIGHTS}")
    print(f"Charger free-at right now: {charger_free_at}")

    best_plan = None
    best_cost = None

    for candidate_plan in candidate_plans:
        print(f"\n  Trying plan {candidate_plan}:")

        current_time = departure_minute
        current_position = 0
        total_wait = 0

        for station_name in candidate_plan:
            station_position = STATIONS[station_name]

            distance_to_drive = station_position - current_position
            arrival_time = current_time + distance_to_drive

            free_time = charger_free_at[station_name]
            if arrival_time >= free_time:
                charge_start = arrival_time
                wait_minutes = 0
            else:
                charge_start = free_time
                wait_minutes = free_time - arrival_time

            total_wait = total_wait + wait_minutes
            charge_end = charge_start + CHARGE_MINUTES
            print(f"    {station_name}: arrive {arrival_time}, "
                  f"wait {wait_minutes}, charge {charge_start}->{charge_end}")

            current_time = charge_end
            current_position = station_position

        distance_to_end = ROUTE_LENGTH - current_position
        predicted_arrival = current_time + distance_to_end
        print(f"    facts: total_wait={total_wait}, "
              f"predicted_arrival={predicted_arrival}")

        # Turn those facts into one cost number using the weights.
        cost = score_plan(total_wait, predicted_arrival)

        # Keep this plan if it's the first one or cheaper than the best so far.
        if best_cost is None or cost < best_cost:
            best_cost = cost
            best_plan = candidate_plan
            print(f"    *** new best plan so far: {best_plan} "
                  f"(cost {best_cost}) ***")

    print(f"\n  CHOSEN plan for {bus_name}: {best_plan} (cost {best_cost})")
    return best_plan


# ---------------------------------------------------------------------------
# STEP 7: evaluate a WHOLE assignment and give it ONE global cost.
# An "assignment" is a dict: bus_name -> the plan that bus will use.
# This function runs EVERY bus through its assigned plan (sharing chargers, so
# buses still wait), collects each bus's timeline, and then measures the whole
# schedule with the three real metrics. This is the thing local search needs:
# to compare two assignments we need one number for each.
#
# Why this fixes the old "overall" smell: total_wait and makespan can only be
# known AFTER all buses are placed, so we compute them here on the full result,
# not on a single bus's guess.
# ---------------------------------------------------------------------------
def evaluate_assignment(buses, assignment):
    """Simulate every bus on its assigned plan; return (timelines, global_cost).

    `buses` is the list of {"name", "departure"} dicts.
    `assignment` maps bus_name -> chosen plan (list of stations).
    """
    print("\n========= EVALUATING A FULL ASSIGNMENT =========")
    for bus in buses:
        print(f"  {bus['name']} -> {assignment[bus['name']]}")

    # Fresh chargers: every station free at minute 0.
    charger_free_at = {}
    for station_name in STATIONS:
        charger_free_at[station_name] = 0

    # Earliest-departing bus claims chargers first.
    buses_in_order = sorted(buses, key=lambda one_bus: one_bus["departure"])

    all_timelines = []

    for bus in buses_in_order:
        bus_name = bus["name"]
        chosen_plan = assignment[bus_name]

        current_time = bus["departure"]
        current_position = 0
        total_wait = 0

        for station_name in chosen_plan:
            station_position = STATIONS[station_name]

            distance_to_drive = station_position - current_position
            arrival_time = current_time + distance_to_drive

            free_time = charger_free_at[station_name]
            if arrival_time >= free_time:
                charge_start = arrival_time
                wait_minutes = 0
            else:
                charge_start = free_time
                wait_minutes = free_time - arrival_time

            total_wait = total_wait + wait_minutes
            charge_end = charge_start + CHARGE_MINUTES
            charger_free_at[station_name] = charge_end

            current_time = charge_end
            current_position = station_position

        distance_to_end = ROUTE_LENGTH - current_position
        final_arrival = current_time + distance_to_end

        print(f"  {bus_name}: total_wait={total_wait}, arrival={final_arrival}")

        all_timelines.append({
            "name": bus_name,
            "plan": chosen_plan,
            "total_wait": total_wait,
            "final_arrival": final_arrival,
        })

    # ---- Now measure the WHOLE schedule with three real, global metrics. ----
    # Metric 1: total wait across every bus (the network's total idle pain).
    total_wait_all = 0
    for timeline in all_timelines:
        total_wait_all = total_wait_all + timeline["total_wait"]

    # Metric 2: the single worst wait any one bus suffered (fairness to one bus).
    worst_individual_wait = 0
    for timeline in all_timelines:
        if timeline["total_wait"] > worst_individual_wait:
            worst_individual_wait = timeline["total_wait"]

    # Metric 3: makespan = when the LAST bus finally arrives (whole-network time).
    makespan = 0
    for timeline in all_timelines:
        if timeline["final_arrival"] > makespan:
            makespan = timeline["final_arrival"]

    print("\n  --- global metrics for this assignment ---")
    print(f"  total_wait_all       = {total_wait_all}")
    print(f"  worst_individual_wait = {worst_individual_wait}")
    print(f"  makespan             = {makespan}")

    # Combine them into ONE cost using the weights (the single number to minimise).
    global_cost = (
        WEIGHTS["individual_wait"] * worst_individual_wait
        + WEIGHTS["overall"] * total_wait_all
        + WEIGHTS["individual_arrival"] * makespan
    )
    print(f"  => GLOBAL COST = {global_cost}\n")

    return all_timelines, global_cost


# ---------------------------------------------------------------------------
# STEP 8: LOCAL SEARCH (1-swap hill climbing).
# Start from the greedy assignment, then repeatedly look at every "neighbor"
# (the same assignment with ONE bus moved to one of its OTHER plans), move to
# the single best neighbor that lowers the global cost, and repeat. When a full
# pass finds NO improving neighbor we are at a local minimum and we stop.
#
# The neighborhood is LINEAR (buses * (plans-1)), not the exponential whole
# space (plans ** buses) -- that is exactly why this is affordable.
# ---------------------------------------------------------------------------
def generate_neighbor_moves(current_assignment, candidate_plans):
    """List every 1-swap move as a (bus_name, new_plan) pair.

    A move means: change THIS one bus to THIS other plan, leave everyone else.
    We skip a bus's own current plan (that is not a change).
    """
    neighbor_moves = []
    for bus_name in current_assignment:
        current_plan = current_assignment[bus_name]
        for candidate_plan in candidate_plans:
            if candidate_plan != current_plan:
                neighbor_moves.append((bus_name, candidate_plan))
    return neighbor_moves


def local_search(buses, candidate_plans):
    """Polish the greedy schedule by repeatedly taking the best 1-swap."""
    print("\n@@@@@@@@@@@@@@@ LOCAL SEARCH @@@@@@@@@@@@@@@")

    # 1) INITIAL assignment = whatever greedy produced. Each bus's chosen plan.
    greedy_timelines = run_scheduler(buses, candidate_plans)
    current_assignment = {}
    for timeline in greedy_timelines:
        current_assignment[timeline["name"]] = timeline["plan"]

    print("\n  INITIAL (greedy) assignment:")
    for bus_name in current_assignment:
        print(f"    {bus_name} -> {current_assignment[bus_name]}")

    # Measure where greedy left us.
    _, current_cost = evaluate_assignment(buses, current_assignment)
    print(f"\n  starting global cost = {current_cost}")

    pass_number = 0
    while True:
        pass_number = pass_number + 1
        print(f"\n  =============== LOCAL SEARCH PASS {pass_number} ===============")

        neighbor_moves = generate_neighbor_moves(
            current_assignment, candidate_plans
        )
        print(f"  this pass has {len(neighbor_moves)} neighbor moves to try")

        # Track the single best improving move found in this whole pass.
        best_move = None
        best_move_cost = current_cost

        for move in neighbor_moves:
            bus_to_change = move[0]
            new_plan = move[1]

            # Build the neighbor: copy the assignment, change ONE bus.
            neighbor_assignment = dict(current_assignment)
            neighbor_assignment[bus_to_change] = new_plan

            print(f"\n    -- trying move: {bus_to_change} -> {new_plan}")
            _, neighbor_cost = evaluate_assignment(buses, neighbor_assignment)
            print(f"    neighbor cost = {neighbor_cost} "
                  f"(best improver so far = {best_move_cost})")

            if neighbor_cost < best_move_cost:
                best_move_cost = neighbor_cost
                best_move = move
                print(f"    *** NEW BEST improving move: "
                      f"{bus_to_change} -> {new_plan} (cost {neighbor_cost}) ***")

        # End of pass: did anything beat where we started?
        if best_move is None:
            print(f"\n  no improving move in pass {pass_number} "
                  f"-> LOCAL MINIMUM reached, stopping")
            break

        # Apply the single best move and loop again from the new assignment.
        bus_to_change = best_move[0]
        new_plan = best_move[1]
        print(f"\n  >>> APPLYING best move of pass {pass_number}: "
              f"{bus_to_change} -> {new_plan}")
        print(f"  global cost {current_cost} -> {best_move_cost}")
        current_assignment[bus_to_change] = new_plan
        current_cost = best_move_cost

    print("\n  FINAL assignment after local search:")
    for bus_name in current_assignment:
        print(f"    {bus_name} -> {current_assignment[bus_name]}")
    print(f"  FINAL global cost = {current_cost}")
    return current_assignment, current_cost


# ---------------------------------------------------------------------------
# STEP 9: the FIRST CP-SAT model (the smallest one that teaches the idea).
#
# Everything above is IMPERATIVE: we wrote HOW to build a schedule (drive, wait,
# charge, loop). CP-SAT is DECLARATIVE: we DECLARE variables + constraints that
# describe what a VALID, GOOD schedule looks like, and the solver's branch-and-
# bound engine finds the provably OPTIMAL one for us.
#
# A Python "if arrival >= free_time" CANNOT go inside a CP-SAT model. The solver
# needs the rule "two buses can't use the one charger at the same time" stated as
# a CONSTRAINT. The standard tool is an INTERVAL variable per bus + AddNoOverlap.
#
# To keep this first step small we model the SMALLEST meaningful problem:
#   one station, one charger, several buses, each must charge CHARGE_MINUTES,
#   a bus cannot start charging before it ARRIVES, only one bus charges at a
#   time, and we MINIMIZE the total waiting time across all buses.
# ---------------------------------------------------------------------------
def cpsat_single_station(arrivals_by_bus):
    """Optimally order buses on ONE charger to minimise total wait.

    `arrivals_by_bus` maps bus_name -> the minute that bus ARRIVES at the station.
    Returns the optimal (start_minute_by_bus, total_wait).
    """
    print("\n############## CP-SAT: ONE STATION, ONE CHARGER ##############")
    for bus_name in arrivals_by_bus:
        print(f"  {bus_name} arrives at minute {arrivals_by_bus[bus_name]}")

    # A horizon = an upper bound on time, so the solver has a finite window.
    # Worst case: everyone queues one after another, so sum of all charges plus
    # the latest arrival is always enough room.
    latest_arrival = 0
    for bus_name in arrivals_by_bus:
        if arrivals_by_bus[bus_name] > latest_arrival:
            latest_arrival = arrivals_by_bus[bus_name]
    horizon = latest_arrival + CHARGE_MINUTES * len(arrivals_by_bus)
    print(f"  horizon (time window for the solver) = {horizon}")

    # The model holds all our variables and constraints.
    model = cp_model.CpModel()

    # For each bus we create THREE linked things:
    #   start_var  : the minute charging starts (a decision the solver makes)
    #   end_var    : start + CHARGE_MINUTES
    #   interval   : a block [start, end) used by the no-overlap rule
    start_var_by_bus = {}
    interval_by_bus = {}
    wait_var_by_bus = {}

    for bus_name in arrivals_by_bus:
        arrival_minute = arrivals_by_bus[bus_name]

        # start can be anywhere from this bus's arrival up to the horizon.
        start_var = model.new_int_var(arrival_minute, horizon,
                                      f"start_{bus_name}")
        end_var = model.new_int_var(arrival_minute,
                                    horizon + CHARGE_MINUTES,
                                    f"end_{bus_name}")
        interval_var = model.new_interval_var(start_var, CHARGE_MINUTES,
                                              end_var, f"interval_{bus_name}")

        # wait = how long after arriving the bus actually starts charging.
        wait_var = model.new_int_var(0, horizon, f"wait_{bus_name}")
        model.add(wait_var == start_var - arrival_minute)

        start_var_by_bus[bus_name] = start_var
        interval_by_bus[bus_name] = interval_var
        wait_var_by_bus[bus_name] = wait_var

    # THE key constraint: one charger -> the intervals may not overlap.
    all_intervals = []
    for bus_name in interval_by_bus:
        all_intervals.append(interval_by_bus[bus_name])
    model.add_no_overlap(all_intervals)
    print("  added AddNoOverlap: the single charger can hold one bus at a time")

    # THE objective: make the sum of all waits as small as possible.
    all_wait_vars = []
    for bus_name in wait_var_by_bus:
        all_wait_vars.append(wait_var_by_bus[bus_name])
    model.minimize(sum(all_wait_vars))
    print("  objective: minimise total wait across all buses")

    # Hand the model to the solver (this is the branch-and-bound engine).
    solver = cp_model.CpSolver()
    status = solver.solve(model)
    print(f"\n  solver status = {solver.status_name(status)}")

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        print("  no solution found")
        return None, None

    start_minute_by_bus = {}
    total_wait = 0
    for bus_name in arrivals_by_bus:
        chosen_start = solver.value(start_var_by_bus[bus_name])
        chosen_wait = solver.value(wait_var_by_bus[bus_name])
        start_minute_by_bus[bus_name] = chosen_start
        total_wait = total_wait + chosen_wait
        print(f"  {bus_name}: arrive {arrivals_by_bus[bus_name]}, "
              f"start {chosen_start}, wait {chosen_wait}, "
              f"end {chosen_start + CHARGE_MINUTES}")

    print(f"\n  OPTIMAL total wait = {total_wait}")
    print(f"  (proven optimal: {status == cp_model.OPTIMAL})")
    return start_minute_by_bus, total_wait


if __name__ == "__main__":
    all_plans = feasible_plans()

    # Step 8 demo: let local search start from the greedy schedule and polish it
    # with 1-swap moves until no swap improves the global cost. Several buses
    # depart close together so they fight over chargers -- giving local search
    # something to fix. Watch the global cost tick DOWN across passes.
    buses = [
        {"name": "bus-BK-01", "departure": 0},
        {"name": "bus-BK-02", "departure": 5},
        {"name": "bus-BK-03", "departure": 10},
        {"name": "bus-BK-04", "departure": 15},
    ]

    local_search(buses, all_plans)

    # Step 9 demo: the smallest CP-SAT model. Three buses arrive at ONE station
    # close together; there is ONE charger. The solver finds the order that
    # minimises total waiting and PROVES it optimal. Change the arrival minutes
    # and watch the optimal ordering / waits change.
    arrivals_by_bus = {
        "bus-01": 0,
        "bus-02": 10,
        "bus-03": 15,
    }
    cpsat_single_station(arrivals_by_bus)
