"""ws/admin/live-locations/ (and its alias ws/admin/dashboard/): the live map socket.

WHAT THIS REPLACES
------------------
The old consumer checked `is_staff`, joined the `admin_dashboard` group and
forwarded every driver frame to every admin: unscoped, with the driver's name,
phone and plate on it, authenticated by a JWT in the URL. See `tickets.py`
for the credential, `stream.py` for the per-frame filter and `scope.py` for
the places.

CONNECT, IN ORDER
-----------------
  1. `?ticket=` present, known and unused (GETDEL)    else close 4001
  2. the ticket's user is an active operator           else close 4001
  3. holds `liveops.read` or `liveops.aggregate`       else close 4003
  4. accept, audit the OPEN, send `hello`, and (read) the initial dots

`?token=` is never read here (the JWT middleware leaves `/ws/admin/` sockets
anonymous). The refusals are sent AFTER accepting, because a browser that is
refused during the handshake sees only code 1006 and cannot tell an expired
ticket from a revoked role; 4001 and 4003 are only visible on an open socket.

WHILE OPEN
----------
A `aggregate` socket does not join the location group at all, so it cannot
receive a driver frame by construction; it gets a hello and city counts.
A `full` socket gets one coalesced frame per driver per second.
Every `SCOPE_RECHECK_SECONDS` the capabilities and places are re-read from
the database; a revoked assignment ends the stream (close 4003), a narrowed
one removes the dots that fell out of scope.
"""

import asyncio
import contextlib
import json
import logging
import time
import uuid
from urllib.parse import parse_qs

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer

from . import audit
from . import config as cfg
from .scope import FULL, LiveScope, coordinates_exact
from .stream import FrameFilter
from .tickets import TicketStoreUnavailable, consume_ticket

logger = logging.getLogger(__name__)

GROUP = 'admin_dashboard'


class AdminDashboardConsumer(AsyncWebsocketConsumer):

    # --- connect --------------------------------------------------------------

    async def connect(self):
        self.user = None
        self.scope_ = None
        self.filter = None
        self.session_id = uuid.uuid4().hex[:12]
        self._opened_at = time.monotonic()
        self._frames = 0
        self._finished = False
        self._joined = False
        self._task = None
        self._audit_request = None

        query = parse_qs(self.scope.get('query_string', b'').decode('utf-8', 'replace'))
        ticket = (query.get('ticket') or [''])[0]
        if not ticket:
            # Includes a client still sending ?token=: refused, not served.
            logger.info('live_ops ws refused 4001: no ticket (token_sent=%s)', 'token' in query)
            return await self._refuse(cfg.CLOSE_NO_TICKET, 'ticket required')

        try:
            user_id = await database_sync_to_async(consume_ticket)(ticket)
        except TicketStoreUnavailable:
            logger.error('live_ops ws: ticket store unavailable')
            return await self._refuse(cfg.CLOSE_UNAVAILABLE, 'try again')
        if user_id is None:
            logger.info('live_ops ws refused 4001: ticket unknown, expired or already used')
            return await self._refuse(cfg.CLOSE_NO_TICKET, 'ticket unknown, expired or used')

        self.user = await self._load_user(user_id)
        if self.user is None:
            return await self._refuse(cfg.CLOSE_NO_TICKET, 'account not active')

        self.scope_ = await database_sync_to_async(LiveScope.of)(self.user)
        if not self.scope_:
            logger.info('live_ops ws refused 4003: user %s holds no live map capability', user_id)
            return await self._refuse(cfg.CLOSE_NO_CAPABILITY, 'no live map capability')

        self._exact = await database_sync_to_async(coordinates_exact)(self.user)
        self.filter = FrameFilter(self.scope_, self._exact)
        self._audit_request = audit.request_like(self.user, self.scope)

        await self.accept()
        await database_sync_to_async(audit.session_opened)(
            self._audit_request, session_id=self.session_id, scope=self.scope_,
            cities=await database_sync_to_async(self._city_codes)())
        await self._hello_and_initial()
        if self.scope_.mode == FULL:
            await self._join()
        self._last_recheck = time.monotonic()
        self._last_status = time.monotonic()
        self._task = asyncio.create_task(self._run())

    async def _refuse(self, code, reason):
        await self.accept()
        await self.close(code=code, reason=reason)

    @database_sync_to_async
    def _load_user(self, user_id):
        from django.contrib.auth import get_user_model

        from base.permissions import is_operator

        user = get_user_model().objects.filter(pk=user_id, is_active=True).first()
        return user if user is not None and is_operator(user) else None

    def _city_codes(self):
        from .geo import Geography
        from .snapshot import _zone_node_ids, visible_cities

        return [c.code for c in visible_cities(self.scope_, Geography.get(), _zone_node_ids())]

    @database_sync_to_async
    def _first_frames(self):
        from .geo import Geography
        from .positions import place, read_live_positions
        from .snapshot import _zone_node_ids, city_status_rows, dots_for, visible_cities

        geography = Geography.get()
        cities = visible_cities(self.scope_, geography, _zone_node_ids())
        positions = read_live_positions()
        placed = place(positions, geography) if positions is not None else []
        dots, total = (dots_for(self.scope_, placed, cities, None, self._exact)
                       if self.scope_.can_read else ([], 0))
        return dots, total, city_status_rows(cities, placed), cities, placed

    async def _hello_and_initial(self):
        dots, total, status_rows, cities, placed = await self._first_frames()
        await self._send({
            'type': 'hello', 'mode': self.scope_.mode, 'session': self.session_id,
            'recheck_seconds': cfg.SCOPE_RECHECK_SECONDS,
            'frame_interval_seconds': cfg.STREAM_FRAME_INTERVAL_SECONDS,
            'cities': [c.code for c in cities],
        })
        if self.scope_.mode == FULL:
            geography_cities = {c.id: c for c in cities}
            for p in placed:
                city = geography_cities.get(p.city.id) if p.city else None
                if (city is not None and self.scope_.reads(city)) or (
                        p.city is None and self.scope_.read is None):
                    self.filter.visible[p.driver_id] = city
            await self._send({'type': 'initial_drivers', 'drivers': dots,
                              'truncated': total > len(dots)})
        await self._send({'type': 'city_status', 'cities': status_rows})

    async def _join(self):
        if not self._joined:
            await self.channel_layer.group_add(GROUP, self.channel_name)
            self._joined = True

    async def _leave(self):
        if self._joined:
            self._joined = False
            await self.channel_layer.group_discard(GROUP, self.channel_name)

    async def _send(self, payload):
        self._frames += 1
        await self.send(text_data=json.dumps(payload))

    # --- the loop -------------------------------------------------------------

    async def _run(self):
        try:
            while True:
                await asyncio.sleep(cfg.STREAM_FRAME_INTERVAL_SECONDS)
                now = time.monotonic()
                if now - self._last_recheck >= cfg.SCOPE_RECHECK_SECONDS:
                    self._last_recheck = now
                    if not await self._recheck():
                        return
                if self.scope_.mode == FULL:
                    frames = await database_sync_to_async(self.filter.flush)()
                    for frame in frames:
                        await self._send(frame)
                if now - self._last_status >= cfg.STREAM_STATUS_INTERVAL_SECONDS:
                    self._last_status = now
                    await self._send({'type': 'city_status', 'cities': await self._status_rows()})
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception('live_ops ws loop failed; closing')
            await self._finish('error')
            await self.close(code=1011)

    @database_sync_to_async
    def _status_rows(self):
        from .geo import Geography
        from .positions import place, read_live_positions
        from .snapshot import _zone_node_ids, city_status_rows, visible_cities

        geography = Geography.get()
        cities = visible_cities(self.scope_, geography, _zone_node_ids())
        positions = read_live_positions()
        return city_status_rows(cities, place(positions, geography) if positions is not None else [])

    async def _recheck(self):
        """Re-read who this is and what they hold. False = the stream ended."""
        user = await self._load_user(self.user.pk)
        scope = await database_sync_to_async(LiveScope.of)(user) if user is not None else None
        if not scope:
            logger.info('live_ops ws: user %s lost the live map capability; closing', self.user.pk)
            await self._finish('revoked')
            await self.close(code=cfg.CLOSE_NO_CAPABILITY, reason='access revoked')
            return False
        self.user = user
        changed = scope != self.scope_
        self.scope_ = scope
        if changed:
            for frame in self.filter.rescope(scope):
                await self._send(frame)
            if scope.mode == FULL:
                await self._join()
            else:
                await self._leave()
        return True

    # --- group events (from DriverLocationConsumer) -----------------------------

    async def driver_location_update(self, event):
        # An aggregate socket is not in the group; this guards a stale join.
        if self.filter is not None and self.scope_ is not None and self.scope_.mode == FULL:
            self.filter.offer_location(event)

    async def driver_status_update(self, event):
        if self.filter is not None and self.scope_ is not None and self.scope_.mode == FULL:
            self.filter.offer_status(event)

    async def receive(self, text_data=None, bytes_data=None):
        pass  # the map sends nothing

    # --- disconnect -------------------------------------------------------------

    async def _finish(self, reason):
        """Write the CLOSE audit row once, whichever path ends the session."""
        if self._finished or self.user is None or self._audit_request is None:
            self._finished = True
            return
        self._finished = True
        await database_sync_to_async(audit.session_closed)(
            self._audit_request, session_id=self.session_id, scope=self.scope_,
            seconds=time.monotonic() - self._opened_at, reason=reason, frames=self._frames)

    async def disconnect(self, close_code):
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        await self._leave()
        await self._finish('closed')
