"""Where every live driver is right now, from Redis, without a name on it.

`redis_client.get_all_active_drivers` already does this, and also loads each
driver's name, phone, plate and rating from Postgres for the old fleet
monitor. The map never needs those, so this reads the minimum: the position
key, the presence heartbeat, the active-trip key and the cached vehicle class.

A driver on a trip is not in the geo index (dispatch must not offer them a
ride), so the geo index cannot be the source. `driver:livepos:<id>` is
written next to the heartbeat on every ping (`redis_client.add_driver_location`).
"""

import json
import logging
from dataclasses import dataclass
from typing import Optional

from .config import LIVEPOS_PREFIX
from .dots import ON_TRIP, ONLINE

logger = logging.getLogger(__name__)

_CHUNK = 500


@dataclass
class LivePosition:
    driver_id: int
    lat: float
    lng: float
    heading: Optional[float]
    ts: Optional[float]
    status: str = ONLINE
    trip_id: Optional[int] = None
    vehicle_class: Optional[str] = None
    city: object = None          # geo.Node or None, filled by `place`


def encode(lat, lng, heading, ts):
    return json.dumps([lat, lng, heading, ts], separators=(',', ':'))


def _decode(raw):
    try:
        lat, lng, heading, ts = json.loads(raw)
        return float(lat), float(lng), heading, ts
    except (TypeError, ValueError):
        return None


def read_live_positions(client=None):
    """Every driver with a live heartbeat and a position, as `LivePosition`s.

    Returns `None` (not an empty list) when Redis is unreachable, so "nobody
    is online" and "we cannot tell" stay different answers.
    """
    from servers.redis_client import (
        ACTIVE_TRIP_PREFIX, HEARTBEAT_PREFIX, VEHICLE_TYPE_PREFIX, redis_client,
    )

    client = client or redis_client
    if client is None:
        return None
    found = []
    try:
        keys = list(client.scan_iter(match=f'{LIVEPOS_PREFIX}*', count=_CHUNK))
        for start in range(0, len(keys), _CHUNK):
            chunk = keys[start:start + _CHUNK]
            values = client.mget(chunk)
            ids = []
            for key in chunk:
                try:
                    ids.append(int(key[len(LIVEPOS_PREFIX):]))
                except ValueError:
                    ids.append(None)
            pipe = client.pipeline(transaction=False)
            for driver_id in ids:
                pipe.exists(f'{HEARTBEAT_PREFIX}{driver_id}')
                pipe.get(f'{ACTIVE_TRIP_PREFIX}{driver_id}')
                pipe.get(f'{VEHICLE_TYPE_PREFIX}{driver_id}')
            answers = pipe.execute()
            for i, driver_id in enumerate(ids):
                raw = values[i]
                alive, trip, vclass = answers[3 * i:3 * i + 3]
                if driver_id is None or not raw or not alive:
                    continue
                decoded = _decode(raw)
                if decoded is None:
                    continue
                lat, lng, heading, ts = decoded
                trip_id = int(trip) if trip and str(trip).isdigit() else None
                found.append(LivePosition(
                    driver_id=driver_id, lat=lat, lng=lng, heading=heading, ts=ts,
                    status=ON_TRIP if trip_id else ONLINE, trip_id=trip_id,
                    vehicle_class=(vclass or None),
                ))
    except Exception:  # noqa: BLE001 - a Redis failure must not 500 the map
        logger.exception('live_ops: reading live positions failed')
        return None
    return found


def driver_facts(driver_ids):
    """`{driver_id: (home_city_id, vehicle_class)}`. One query; the only
    database read on the live path."""
    from servers.driver.models import Driver

    if not driver_ids:
        return {}
    rows = Driver.objects.filter(id__in=list(driver_ids)).values_list(
        'id', 'home_city_id', 'active_vehicle__vehicle_type_id__type')
    return {did: (home, vclass) for did, home, vclass in rows}


def place(positions, geography):
    """Fill in each position's city: the zone it is in, else the home city.

    Returns the positions (mutated). A position with no city is unplaceable
    and, in `scope`, visible to the unrestricted only.
    """
    facts = driver_facts({p.driver_id for p in positions})
    for p in positions:
        home_id, vclass = facts.get(p.driver_id, (None, None))
        p.vehicle_class = p.vehicle_class or vclass
        p.city = geography.city_at(p.lat, p.lng) or (
            geography.node(home_id) if home_id else None)
    return positions
