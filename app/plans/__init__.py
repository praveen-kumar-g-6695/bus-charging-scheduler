"""The plans layer: enumerating each trip's legal charging options.

A *plan* is the set of stations where a single trip chooses to charge. This
layer answers ONE narrow question -- "which plans are even physically legal for
this trip?" -- using only geometry and the bus's range. It knows nothing about
other trips, chargers, waiting, or weights; those belong to later layers.

Keeping plan generation isolated like this is deliberate: the legal menu for a
trip never changes as the rest of the world grows, so it is the most stable,
most reusable piece of the engine (greedy, local search and any oracle all draw
their candidate plans from here).
"""
