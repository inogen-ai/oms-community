"""Activity: what this instance has done, over time.

Two event types with opposite lifecycles. `AdminEvent` is persisted, because
person administration overwrites the `Person` row and leaves nothing behind.
`ActivityEvent` is a read model assembled per query from sources that already
record - transactions, resolved review items, bindings - and is never stored,
because storing it would be writing a second copy of a fact the graph already
holds and would start empty on the day it shipped.
"""
