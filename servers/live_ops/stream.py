"""The per-frame path: what one admin socket does with the fleet's location frames.

THE PROBLEM
-----------
Every driver ping is published once to the `admin_dashboard` channel group, and
every admin socket receives every ping. The old consumer forwarded them all,
unscoped, with the driver's name, phone and plate riding along.

WHAT EACH SOCKET DOES NOW
-------------------------
  * `offer_location` only REMEMBERS the latest frame per driver (a dict write;
    nothing is sent, resolved or queried), so a burst of pings from one driver
    costs one slot, and the dispatch loop never waits on the client.
  * once a second, `flush` takes everything remembered and, per driver,
    resolves the city (the zone containing the position, else the home city),
    asks the socket's scope whether it may READ that city, and only then
    builds a dot from a whitelist (`dots.build_dot`). A driver in a city the
    socket does not hold is dropped there: no frame, no count, and nothing
    that says they exist.
  * a driver the socket was told about who moves out of its scope, or goes
    offline, gets a removal so the dot does not linger.

`flush` and `rescope` are synchronous and touch the database (one query for
drivers not seen before); the consumer runs them in a thread.
"""

import time

from . import config
from .dots import OFFLINE, build_dot, status_of
from .geo import Geography
from .positions import driver_facts

#: Per-socket cap on remembered-but-unsent frames. A fleet larger than this
#: is past what one socket could draw anyway.
MAX_PENDING = 20000
#: How long a driver's home city and vehicle class are trusted.
FACTS_TTL_SECONDS = 300


def removal(driver_id):
    return {'type': 'driver_status_update', 'driver_id': int(driver_id), 'status': OFFLINE}


class FrameFilter:
    def __init__(self, scope, exact, geography=None):
        self.scope = scope
        self.exact = exact
        self._geography = geography
        self.pending = {}      # driver_id -> latest raw frame
        self.visible = {}      # driver_id -> city Node|None the socket was told about
        self.gone = set()      # drivers reported offline since the last flush
        self._facts = {}       # driver_id -> (home_city_id, vehicle_class, loaded_at)
        self.dropped_out_of_scope = 0

    def geography(self):
        return self._geography or Geography.get()

    # --- called from the dispatch loop: cheap, never blocks ------------------

    def offer_location(self, event):
        try:
            driver_id = int(event['driver_id'])
            lat, lng = float(event['lat']), float(event['lng'])
        except (KeyError, TypeError, ValueError):
            return
        if driver_id not in self.pending and len(self.pending) >= MAX_PENDING:
            return
        # Only what a dot is built from. The frame's name, phone, plate and
        # rating are not copied, so nothing downstream can leak them.
        self.pending[driver_id] = {
            'lat': lat, 'lng': lng, 'heading': event.get('heading'),
            'status': event.get('status'), 'trip_id': event.get('active_trip_id'),
            'ts': time.time(),
        }
        self.gone.discard(driver_id)

    def offer_status(self, event):
        try:
            driver_id = int(event['driver_id'])
        except (KeyError, TypeError, ValueError):
            return
        if status_of(event.get('status')) == OFFLINE:
            self.pending.pop(driver_id, None)
            if driver_id in self.visible:
                self.gone.add(driver_id)

    # --- once a second, in a thread --------------------------------------------

    def _load_facts(self, driver_ids):
        now = time.monotonic()
        stale = [d for d in driver_ids
                 if d not in self._facts or now - self._facts[d][2] > FACTS_TTL_SECONDS]
        if stale:
            known = driver_facts(stale)
            for d in stale:
                home, vclass = known.get(d, (None, None))
                self._facts[d] = (home, vclass, now)

    def flush(self):
        """Frames to send now: dots for readable drivers, removals for the rest."""
        pending, self.pending = self.pending, {}
        frames = []
        if pending:
            geography = self.geography()
            self._load_facts(list(pending))
            for driver_id, raw in pending.items():
                home_id, vclass, _loaded = self._facts[driver_id]
                city = geography.city_at(raw['lat'], raw['lng']) or (
                    geography.node(home_id) if home_id else None)
                if not self._may_read(city):
                    self.dropped_out_of_scope += 1
                    if driver_id in self.visible:       # moved out of scope
                        del self.visible[driver_id]
                        frames.append(removal(driver_id))
                    continue
                self.visible[driver_id] = city
                frames.append({'type': 'driver_location_update', **build_dot(
                    driver_id=driver_id, status=raw['status'], lat=raw['lat'], lng=raw['lng'],
                    vehicle_class=vclass, heading=raw['heading'], updated_at=raw['ts'],
                    trip_id=raw['trip_id'], city=city.code if city else None,
                    exact=self.exact)})
        for driver_id in sorted(self.gone):
            if self.visible.pop(driver_id, 0) != 0:
                frames.append(removal(driver_id))
        self.gone.clear()
        return frames

    def _may_read(self, city):
        return self.scope.reads(city) if city is not None else self.scope.read is None

    # --- when the socket's scope is re-read ----------------------------------

    def rescope(self, scope):
        """Adopt a new scope. Returns removals for every driver the socket was
        told about that it may no longer read."""
        self.scope = scope
        frames = []
        for driver_id, city in list(self.visible.items()):
            if not scope.can_read or not self._may_read(city):
                del self.visible[driver_id]
                frames.append(removal(driver_id))
        if not scope.can_read:
            self.pending.clear()
        return frames
