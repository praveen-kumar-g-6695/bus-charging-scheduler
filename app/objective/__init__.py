"""The objective layer: collapsing many weighted rules into one score.

The simulator says what a schedule IS; the rules say how bad it is along each
separate axis; this layer says how bad it is OVERALL, as the single number the
schedulers actually minimise. Keeping that collapse in its own tiny module means
the schedulers depend on one stable thing -- "give me the cost of this result" --
and never on how many rules exist or how they are weighted.
"""
