"""The scheduling layer: strategies that decide each trip's charging plan.

Everything before this layer was mechanism: the plan generator lists each trip's
legal options, the simulator turns a full set of choices into a concrete
schedule, and the cost function scores that schedule. This layer is POLICY -- it
actually CHOOSES, for every trip, which feasible plan to commit to so the cost
comes out low.

Different algorithms can make that choice (a fast greedy pass, a local-search
refinement, an exact oracle), so the choosing is modelled as a Strategy: one
``SchedulerStrategy`` interface, many interchangeable implementations. The rest of
the app depends only on the interface, so swapping or adding a strategy never
ripples outward.
"""
