"""Which city is a point in: ONE answer for the snapshot, the stream and the sampler.

A driver's city on the live map is the `geo_node` of the zone containing their
CURRENT position, falling back to their `home_city` (owner delegation, module
10). It is not the home city alone: a Hyderabad driver working an Austin trip
is Austin's driver for the length of the trip, and Austin's operators are the
ones who need to see them.

The zone index is rebuilt at most once per `TTL` seconds. The per-frame path
asks it about every driver every second, so it must not touch the database
each time; a zone redrawn in module 9 shows up on the map within a minute.
"""

import threading
import time
from dataclasses import dataclass
from typing import Optional

from servers.admin_access.models import CITY, GeoNode

TTL_SECONDS = 60


@dataclass(frozen=True)
class Node:
    id: int
    code: str
    name: str
    kind: str
    path: str
    status: str


class Geography:
    _instance = None
    _lock = threading.Lock()

    def __init__(self, nodes, index, loaded_at):
        self.nodes = nodes            # id -> Node
        self._by_code = {n.code: n for n in nodes.values()}
        self._index = index           # pricing.services.ZoneIndex
        self.loaded_at = loaded_at

    @classmethod
    def get(cls, force=False):
        now = time.monotonic()
        inst = cls._instance
        if inst is not None and not force and now - inst.loaded_at < TTL_SECONDS:
            return inst
        with cls._lock:
            inst = cls._instance
            if inst is not None and not force and now - inst.loaded_at < TTL_SECONDS:
                return inst
            cls._instance = cls._load()
            return cls._instance

    @classmethod
    def reset(cls):
        with cls._lock:
            cls._instance = None

    @classmethod
    def _load(cls):
        from servers.pricing.services import ZoneIndex

        nodes = {
            n.id: Node(n.id, n.code, n.name, n.kind, n.path, n.status)
            for n in GeoNode.objects.all()
        }
        return cls(nodes, ZoneIndex.load(), time.monotonic())

    # --- lookups ------------------------------------------------------------

    def node(self, node_id) -> Optional[Node]:
        return self.nodes.get(node_id)

    def by_code(self, code) -> Optional[Node]:
        return self._by_code.get(code)

    def cities(self):
        return sorted((n for n in self.nodes.values() if n.kind == CITY), key=lambda n: n.path)

    def city_at(self, lat, lng) -> Optional[Node]:
        """The node of the most specific zone containing the point that is
        linked to a geo node (the SOS anchoring rule), or None."""
        for zone in self._index.containing(lat, lng):
            if zone.geo_node_id:
                node = self.nodes.get(zone.geo_node_id)
                if node is not None:
                    return node
        return None
