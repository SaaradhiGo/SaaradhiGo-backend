"""What a caller may see on the live map: capability, then place.

TWO CAPABILITIES, TWO SETS OF PLACES
------------------------------------
`liveops.read` (individual dots, active trips) and `liveops.aggregate`
(heatmap cells only) are held through assignments, and an operator can hold
one in Hyderabad and the other, or neither, in Austin. So the places are the
assignments whose ROLE CARRIES the capability (`payments.scope.paths_for`),
not every assignment the person has: a Support Agent in Austin who is also a
City Ops Manager in Hyderabad sees Hyderabad's drivers and none of Austin's.

An unplaceable thing (a driver outside every zone with no home city) is
visible to the unrestricted only, never to a city. Same rule as everywhere.
"""

from dataclasses import dataclass

from servers.admin_access.bridge import granted
from servers.payments.scope import paths_for

READ = 'liveops.read'
AGGREGATE = 'liveops.aggregate'

FULL, AGGREGATE_ONLY = 'full', 'aggregate'


def _paths(user, permission):
    """`()` = not held, `None` = held everywhere, else the path prefixes."""
    if not granted(user, permission)[0]:
        return ()
    paths = paths_for(user, permission)
    return None if paths is None else tuple(paths)


def _covers(paths, node):
    if paths is None:
        return True
    if node is None or not paths:
        return False
    return any(node.path.startswith(p) for p in paths)


@dataclass(frozen=True)
class LiveScope:
    read: tuple          # () not held, None everywhere, else path prefixes
    aggregate: tuple

    @classmethod
    def of(cls, user):
        return cls(read=_paths(user, READ), aggregate=_paths(user, AGGREGATE))

    # --- what the caller holds ----------------------------------------------
    @property
    def can_read(self):
        return self.read != ()

    @property
    def can_aggregate(self):
        return self.aggregate != ()

    @property
    def mode(self):
        """`full` streams dots; `aggregate` never does; None holds neither."""
        if self.can_read:
            return FULL
        if self.can_aggregate:
            return AGGREGATE_ONLY
        return None

    @property
    def capability(self):
        return READ if self.can_read else (AGGREGATE if self.can_aggregate else None)

    # --- where --------------------------------------------------------------
    def reads(self, node):
        """May individual positions in `node` be shown?"""
        return self.can_read and _covers(self.read, node)

    def sees(self, node):
        """May `node` appear at all (a city row, a heatmap cell)?"""
        return self.reads(node) or (self.can_aggregate and _covers(self.aggregate, node))

    def describe(self):
        def one(paths):
            if paths == ():
                return None
            return 'all' if paths is None else list(paths)
        return {'read': one(self.read), 'aggregate': one(self.aggregate)}

    def __bool__(self):
        return self.mode is not None


def coordinates_exact(user):
    """May this caller see a position to the metre? Holders of `pii.view`
    (the masking layer's TRUSTED policy); everybody else sees ~1 km."""
    from servers.admin_access.masking import TRUSTED, policy_for

    return policy_for(user) is TRUSTED
