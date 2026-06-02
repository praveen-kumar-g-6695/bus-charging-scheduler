"""The simulation layer: turning chosen plans into a concrete schedule.

The plans layer said which stations each trip *could* charge at. This layer
answers what *actually happens* once those choices meet reality: a finite number
of charger lanes per station, shared by buses coming from BOTH directions, served
first-come-first-served. Its one job is the transform::

    Scenario + (one ChargingPlan per Trip)  --simulate-->  ScheduleResult

It computes nothing about whether a schedule is *good* (that is the cost layer);
it only computes what the clock would really do.
"""
