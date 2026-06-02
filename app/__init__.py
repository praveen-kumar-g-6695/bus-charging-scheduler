"""Bus Charging Scheduler -- production application package.

This package is the production-grade rebuild of the single-file learning script
(main.py, kept frozen as a teaching log). It is organised into clear layers:

    domain/      the data model (enums + Pydantic value objects)
    plans/       per-bus charging-plan generation
    simulation/  the charger + the schedule simulator
    rules/       the pluggable rule registry (add a rule = add a class)
    objective/   the composite weighted cost function
    scheduling/  the swappable scheduler strategies (greedy, local search)
    io/          scenario loading (YAML -> domain)
    ui/          the Streamlit front end

Nothing in this package calls print(); everything logs through app.logging_config.
"""
