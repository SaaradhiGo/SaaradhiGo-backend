import asyncio
import contextlib
import json
import logging
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from django.contrib.auth.models import AnonymousUser
from django.core.cache import cache
from base.utils import generate_otp

logger = logging.getLogger(__name__)

# OTP brute-force lock on the trip-start (rider's OTP entered by driver).
# Cache key: trip_otp_attempts:<trip_id>. Once attempts >= limit, the start
# action is refused until the TTL expires (forces driver to ask rider for
# OTP again rather than enumerating 1M possibilities).
TRIP_OTP_MAX_ATTEMPTS = 5
TRIP_OTP_ATTEMPT_TTL_SECONDS = 5 * 60

# The trip status each lifecycle command produces. The wire protocol has always
# echoed the *verb* back in `trip_status_update.status` ('complete'), which is not
# the status stored in PostgreSQL ('completed'). `command_ack.trip_status` reports
# the durable value instead, so a client can compare an ack against what it reads
# back from the API without translating.
_TARGET_STATUS = {
    'accept': 'accepted',
    'reached': 'reached',
    'start': 'in_progress',
    'complete': 'completed',
    'cancel': 'cancelled',
    # `confirm_cash` deliberately absent. It records that the driver collected the
    # fare; it does NOT move the trip's state machine, which is already
    # `completed` by the time it is allowed to run. Mapping it to a status was a
    # mistake caught in QA: the ack reported `trip_status: in_progress` for a trip
    # that was completed, which is exactly the kind of confident wrong answer this
    # protocol exists to eliminate. Its ack now reports the trip's real status.
}



class LocationBroadcastMixin:
    """Deliver high-frequency location frames without blocking the dispatch loop.

    Channels feeds a consumer's incoming websocket frames *and* its channel-layer
    events into one sequential `await_many_dispatch` loop, and `self.send()` blocks
    for as long as the client is not draining its socket. Forwarding a location
    broadcast with a direct `await self.send(...)` therefore lets ordinary GPS
    traffic stall the *commands* arriving on the same socket.

    That is not hypothetical. Every location frame a driver sends fans out to the
    trip group, so a trip socket receives one `driver_location_update` per ping.
    Against a client that buffers sixteen messages -- the reference `websockets`
    default, and more than a backgrounded mobile app manages -- the seventeenth
    frame blocked the consumer's send, the dispatch loop stopped, and `complete`
    sat unread in the incoming queue. The trip stayed `in_progress` while the
    driver's app, whose send had succeeded, believed the ride was finished. The
    same starvation applies to `cancel` and to an SOS.

    The correction separates the two classes of traffic rather than enlarging a
    buffer. Location frames go onto a depth-1 queue drained by a dedicated task,
    and a newer position *replaces* an older undelivered one: coalescing, not
    buffering, because a superseded position has no value to anyone. A slow client
    now loses intermediate positions -- which is the correct thing to lose -- and
    the dispatch loop stays free. Commands and status frames keep their direct
    `await self.send(...)`: they are rare, they are ordered, and none may be
    dropped.

    Ordering contract, since this changes it:
      * status/command frames stay strictly ordered among themselves, and are
        never delayed behind a location frame;
      * location frames stay ordered among themselves, but may be dropped, and may
        arrive after a status frame that was emitted later.
    """

    async def _start_location_pump(self):
        """Call once, after `accept()`."""
        self._location_queue = asyncio.Queue(maxsize=1)
        self._location_dropped = 0
        self._location_pump_task = asyncio.create_task(self._location_pump())

    async def _stop_location_pump(self):
        """Call from `disconnect()`. Idempotent."""
        task = getattr(self, '_location_pump_task', None)
        if task is None:
            return
        self._location_pump_task = None
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        dropped = getattr(self, '_location_dropped', 0)
        if dropped:
            # Coordinates are never logged; only how many were superseded.
            logger.info('location_frames_coalesced', extra={
                'event': 'location_frames_coalesced',
                'channel': getattr(self, 'channel_name', None),
                'dropped': dropped,
            })

    def _queue_location_frame(self, payload):
        """Hand a location frame to the pump. Never blocks, never raises.

        This is what makes the decoupling real: it must be safe to call from the
        dispatch loop with no possibility of waiting on the client.
        """
        queue = getattr(self, '_location_queue', None)
        if queue is None:
            # Pump never started -- the connection was refused before `accept()`.
            return
        if queue.full():
            try:
                queue.get_nowait()
                self._location_dropped += 1
            except asyncio.QueueEmpty:
                pass
        try:
            queue.put_nowait(payload)
        except asyncio.QueueFull:
            # Raced with another enqueue. The newest position wins next time.
            self._location_dropped += 1

    async def _location_pump(self):
        """Drain the location queue into the socket.

        This task may block for as long as the client likes. Nothing awaits it, so
        that is now a location-delivery problem rather than a lifecycle one.
        """
        while True:
            payload = await self._location_queue.get()
            try:
                await self.send(text_data=json.dumps(payload))
            except asyncio.CancelledError:
                raise
            except Exception:
                # A wedged or closed socket must not take the consumer with it;
                # the disconnect path owns teardown.
                logger.debug('location_frame_undelivered', extra={
                    'event': 'location_frame_undelivered',
                    'channel': getattr(self, 'channel_name', None),
                })
                return


class DriverLocationConsumer(LocationBroadcastMixin, AsyncWebsocketConsumer):
    """
    WebSocket consumer for real-time driver location updates.
    
    Connect: ws://host/ws/driver/location/?token=<jwt>
    Send:    {"lng": 78.4867, "lat": 17.3850}
    """

    async def connect(self):
        self.user = self.scope.get('user')

        if isinstance(self.user, AnonymousUser) or not self.user.is_authenticated:
            logger.info("WS reject 4001: unauthenticated driver socket")
            await self.close(code=4001)
            return

        # Verify user is a driver
        self.driver = await self._get_driver()
        if not self.driver:
            logger.info("WS reject 4003: user has no driver profile")
            await self.close(code=4003)
            return
        if not self.driver.approved:
            await self.close(code=4004)
            return
        lat = self.scope.get('lat')
        lng = self.scope.get('lng')
        if not(lat and lng):
            await self.close(code=4003)
            return
        self.driver_id = self.driver.id
        self.driver_group = f'driver_{self.driver_id}'
        
        # Accept the connection early to prevent client timeout
        await self.accept()
        await self.send(text_data=json.dumps({
            'type': 'connection_established',
            'message': f'Driver {self.driver_id} connected',
        }))
        await self._start_location_pump()

        # GPS ingestion health for this connection. Two integers, not a log line
        # per frame: the question these answer is the operational one -- "why did
        # this ride's GPS trail stop?" -- and before this there was no answer,
        # because a frame that failed to reach Redis produced one warning among
        # hundreds of identical ones and no aggregate anywhere.
        self._gps_accepted = 0
        self._gps_rejected = 0
        self._gps_reject_reason = None

        # Perform slower database/redis operations in the background of the connection
        await self._active_the_driver()

        # Repair ephemeral state against the database before announcing presence.
        # A driver whose app died mid-ride kept a `driver:active_trip:` key with no
        # TTL; add_driver_location() removes such a driver from the geo index, so
        # reconnecting and pinging could never restore them to supply. PostgreSQL
        # decides, and this is where the cache is corrected.
        try:
            outcome = await self._reconcile_active_trip()
            if outcome and outcome != 'ok':
                logger.warning(
                    'driver_active_trip_reconciled driver=%s outcome=%s',
                    self.driver_id, outcome,
                )
        except Exception:  # noqa: BLE001
            logger.exception('active-trip reconciliation failed for driver %s',
                             self.driver_id)

        await self._add_driver_location(lng, lat)
        
        # Join driver's personal group (for receiving ride requests)
        await self.channel_layer.group_add(self.driver_group, self.channel_name)
        # Join global online drivers group
        await self.channel_layer.group_add('online_drivers', self.channel_name)

        # Notify admin dashboard of online driver
        driver_details = await self._get_driver_broadcast_info()
        driver_details.update({
            'type': 'driver_location_update',
            'driver_id': self.driver_id,
            'lat': float(lat),
            'lng': float(lng),
            'status': 'online',
        })
        await self.channel_layer.group_send('admin_dashboard', driver_details)

    async def disconnect(self, close_code):
        await self._stop_location_pump()
        self._log_gps_session(close_code)
        if hasattr(self, 'driver_id'):
            await self._deactive_the_driver()
            # Remove from groups
            await self.channel_layer.group_discard(self.driver_group, self.channel_name)
            await self.channel_layer.group_discard('online_drivers', self.channel_name)
            # Remove from Redis geo index
            await self._remove_driver_location()
            # Notify admin dashboard that driver went offline
            await self.channel_layer.group_send('admin_dashboard', {
                'type': 'driver_status_update',
                'driver_id': self.driver_id,
                'status': 'offline',
            })
            logger.info(f"Driver {self.driver_id} disconnected")


    def _log_gps_session(self, close_code):
        """One line per connection describing what happened to its GPS.

        Emitted at WARNING when any frame was lost, so it survives
        production's log level, and at INFO otherwise. Carries counts and a
        reason code only -- never a coordinate.
        """
        accepted = getattr(self, '_gps_accepted', 0)
        rejected = getattr(self, '_gps_rejected', 0)
        if not accepted and not rejected:
            return
        payload = {
            'event': 'gps_session_summary',
            'driver_id': getattr(self, 'driver_id', None),
            'accepted': accepted,
            'rejected': rejected,
            'close_code': close_code,
        }
        if getattr(self, '_gps_reject_reason', None):
            payload['first_reject_reason'] = self._gps_reject_reason
        if rejected:
            logger.warning('gps_session_summary', extra=payload)
        else:
            logger.info('gps_session_summary', extra=payload)

    async def receive(self, text_data):
        """
        Receive location update from driver.
        Expected: {"lng": float, "lat": float}
        """
        try:
            data = json.loads(text_data)
            lng = data.get('lng')
            lat = data.get('lat')

            if lng is None or lat is None:
                await self.send(text_data=json.dumps({
                    'type': 'error',
                    'message': 'lng and lat are required'
                }))
                return

            # Update location in Redis Geo (using the "smart" checker)
            result = await self._add_driver_location(lng, lat)

            if result.get('success'):
                self._gps_accepted = getattr(self, '_gps_accepted', 0) + 1
                # Queued, not sent: this acknowledgement is emitted per GPS frame,
                # so a driver app that does not read it would otherwise block
                # `receive` once its buffer filled -- silently ending GPS
                # ingestion, and with it the trip's distance evidence, mid-ride.
                self._queue_location_frame({
                    'type': 'location_updated',
                    'lng': lng,
                    'lat': lat,
                })
                
                # Stream location to admin dashboard for real-time fleet monitor
                active_trip_id = result.get('active_trip_id')

                # Durable liveness evidence for the active trip, coalesced to at
                # most one write per interval. Before this, the only record that a
                # ride was alive was a 45-second Redis TTL, so once it expired
                # nothing could distinguish a driver who dropped out a minute ago
                # from one gone for hours -- and an abandoned trip stranded the
                # driver's supply with no way for operations to see it.
                if active_trip_id and active_trip_id != 'unknown':
                    await self._record_trip_liveness(active_trip_id)
                driver_details = await self._get_driver_broadcast_info()
                driver_details.update({
                    'type': 'driver_location_update',
                    'driver_id': self.driver_id,
                    'lat': lat,
                    'lng': lng,
                    'status': 'busy' if active_trip_id else 'online',
                    'active_trip_id': active_trip_id,
                })
                await self.channel_layer.group_send('admin_dashboard', driver_details)

                # If driver is on an active trip, stream location to the rider
                if active_trip_id:
                    await self.channel_layer.group_send(f'trip_{active_trip_id}', {
                        'type': 'driver_location_update',
                        'lng': lng,
                        'lat': lat,
                        'driver_id': self.driver_id,
                    })
            else:
                # A rejected location frame is silent data loss: the driver is
                # still driving, the ride still needs its distance evidence, and
                # nothing downstream will notice the gap. Log the FIRST failure
                # per connection at warning level and count the rest, so a
                # degraded Redis shows up as one event plus a total rather than
                # hundreds of identical lines.
                self._gps_rejected = getattr(self, '_gps_rejected', 0) + 1
                reason = str(result.get('error') or 'unknown')[:120]
                if getattr(self, '_gps_reject_reason', None) is None:
                    self._gps_reject_reason = reason
                    logger.warning('gps_frame_rejected', extra={
                        'event': 'gps_frame_rejected',
                        'driver_id': getattr(self, 'driver_id', None),
                        'reason': reason,
                        'accepted_before': getattr(self, '_gps_accepted', 0),
                    })
                await self.send(text_data=json.dumps({
                    'type': 'error',
                    'message': result.get('error', 'Failed to update location')
                }))

        except json.JSONDecodeError:
            await self.send(text_data=json.dumps({
                'type': 'error',
                'message': 'Invalid JSON'
            }))
        except Exception as e:
            logger.error(f"Error in DriverLocationConsumer.receive: {str(e)}")
            await self.send(text_data=json.dumps({
                'type': 'error',
                'message': 'Internal server error'
            }))

    # -- Event handlers (called via channel layer) --

    async def trip_taken(self, event):
        """Another driver won this ride — dismiss the request card."""
        await self.send(text_data=json.dumps({
            'type': 'trip_taken',
            'trip_id': event['trip_id'],
            'message': 'This ride was accepted by another driver.',
        }))

    async def ride_request(self, event):
        """Send incoming ride request notification to this driver."""
        await self.send(text_data=json.dumps({
            'type': 'ride_request',
            'trip_id': event['trip_id'],
            'rider_name': event.get('rider_name', ''),
            'pickup_lat': event['pickup_lat'],
            'pickup_lng': event['pickup_lng'],
            'destination_lat': event['destination_lat'],
            'destination_lng': event['destination_lng'],
            'pickup_address': event.get('pickup_address', ''),
            'destination_address': event.get('destination_address', ''),
            'estimated_fare': event.get('estimated_fare', ''),
            'distance_km': event.get('distance_km', ''),
            'duration_min': event.get('duration_min', ''),
            'payment_method': event.get('payment_method', ''),
            'vehicle_type': event.get('vehicle_type', ''),
        }))

    # -- Database helpers --

    @database_sync_to_async
    def _get_driver(self):
        try:
            return self.user.driver
        except Exception as e:
            logger.debug("driver lookup failed: %s", e)
            return None

    @database_sync_to_async
    def _get_driver_broadcast_info(self):
        try:
            driver = self.user.driver
            vehicle = getattr(driver, 'active_vehicle', None)
            return {
                'driver_name': str(self.user.full_name or self.user.phone_number or f'Driver {driver.id}'),
                'phone_number': str(self.user.phone_number or ''),
                'ratings': str(driver.ratings or '0.00'),
                'vehicle_model': str(vehicle.model if vehicle else ''),
                'vehicle_number': str(vehicle.vehicle_number if vehicle else ''),
            }
        except Exception as e:
            logger.debug("driver broadcast info lookup failed: %s", e)
            return {
                'driver_name': f'Driver {getattr(self, "driver_id", "")}',
                'phone_number': '',
                'ratings': '0.00',
                'vehicle_model': '',
                'vehicle_number': '',
            }
    @database_sync_to_async
    def _active_the_driver(self):
        """Flip the driver online + open a DriverSession.

        Returns {'ok': True} on success or
        {'ok': False, 'error': ..., 'fatigue': {...}} when the driver
        is in a fatigue / cancel lockout. The WS receive handler should
        check `ok` and reflect the message to the client instead of
        silently accepting the online toggle.
        """
        try:
            from servers.driver.fatigue import get_fatigue_status, record_session_start
            status = get_fatigue_status(self.driver)
            if status.locked:
                return {
                    'ok': False,
                    'error': (
                        'You are temporarily locked out from going online. '
                        f'Try again after {status.locked_until.isoformat() if status.locked_until else "soon"}.'
                    ),
                    'fatigue': status.to_dict(),
                }
            self.driver.status = "online"
            self.driver.save()
            record_session_start(self.driver)
            return {'ok': True, 'fatigue': status.to_dict()}
        except Exception as e:
            logger.error("going online failed: %s", e)
            return {'ok': False, 'error': str(e)}

    @database_sync_to_async
    def _deactive_the_driver(self):
        """Flip the driver offline + close the open DriverSession."""
        try:
            from servers.driver.fatigue import record_session_end
            self.driver.status = "off"
            self.driver.save()
            record_session_end(self.driver, reason='offline')
            return {'ok': True}
        except Exception as e:
            logger.error("going offline failed: %s", e)
            return {'ok': False, 'error': str(e)}
    @database_sync_to_async
    def _update_driver_location(self, lng, lat):
        from servers.driver.utils import update_driver_location
        return update_driver_location(self.driver_id, lng=lng, lat=lat)

    @database_sync_to_async
    def _remove_driver_location(self):
        from servers.redis_client import remove_driver
        return remove_driver(self.driver_id)
    @database_sync_to_async
    def _add_driver_location(self, lng, lat):
        from servers.redis_client import add_driver_location
        return add_driver_location(self.driver_id, lng=lng, lat=lat)

    @database_sync_to_async
    def _record_trip_liveness(self, trip_id):
        from servers.ride.liveness import record_driver_activity
        return record_driver_activity(trip_id)

    @database_sync_to_async
    def _reconcile_active_trip(self):
        """Make Redis agree with the database about this driver's active trip.

        Runs once per socket connect, not per ping. This is what lets a driver who
        crashed mid-ride become dispatchable again on their own: the stale
        `driver:active_trip:` key that kept them out of the geo index is cleared
        here, because PostgreSQL says the trip is over.
        """
        from servers.ride.liveness import reconcile_driver_active_trip
        return reconcile_driver_active_trip(self.driver_id)

class RideRequestConsumer(LocationBroadcastMixin, AsyncWebsocketConsumer):
    """
    WebSocket consumer for riders to request rides and receive updates.
    
    Connect: ws://host/ws/ride/request/?token=<jwt>
    Send:    {
        "pickup_lat": 17.385, "pickup_lng": 78.486,
        "destination_lat": 17.440, "destination_lng": 78.348,
        "pickup_address": "...", "destination_address": "...",
        "vehicle_type": "bike",
    }
    """

    async def connect(self):
        self.user = self.scope.get('user', AnonymousUser())

        if isinstance(self.user, AnonymousUser) or not self.user.is_authenticated:
            await self.close(code=4001)
            return

        self.rider_group = f'rider_{self.user.id}'

        # Join rider's personal group (for receiving trip updates)
        await self.channel_layer.group_add(self.rider_group, self.channel_name)

        await self.accept()
        await self.send(text_data=json.dumps({
            'type': 'connection_established',
            'message': 'Rider connected, ready for ride requests',
        }))
        await self._start_location_pump()

        # Reconnect recovery. Dispatch now outlives this socket, so a rider
        # who dropped mid-search can come back to a trip that has already
        # been accepted — and the real-time frame announcing it went to a
        # socket that no longer existed. Replay authoritative state from
        # PostgreSQL instead of assuming the original event was received.
        snapshot = await self._current_trip_snapshot()
        if snapshot is not None:
            await self.send(text_data=json.dumps({
                'type': 'current_trip',
                'trip': snapshot,
            }))

    async def disconnect(self, close_code):
        # Deliberately does NOT cancel dispatch. This method used to call
        # `self._dispatch_task.cancel()`, which meant a rider losing network
        # mid-search killed the remaining waves and the trip then timed out
        # as `no_driver_accepted` though most drivers were never asked.
        # Celery owns wave execution now; this socket going away is not an
        # instruction to stop looking for a driver.
        await self._stop_location_pump()
        if hasattr(self, 'rider_group'):
            await self.channel_layer.group_discard(self.rider_group, self.channel_name)

    async def receive(self, text_data):
        """
        Receive messages from rider.
        New request: {"pickup_lat": ..., "pickup_lng": ..., "destination_lat": ..., "destination_lng": ..., "vehicle_type": ..., "distance_km": ..., "duration_min": ...}
        Retry:       {"action": "retry", "trip_id": <id>, "radius": <optional, meters>}
        """
        try:
            data = json.loads(text_data)
            action = data.get('action', 'request')

            if action == 'retry':
                await self._handle_retry(data)
                return

            # --- New ride request flow ---
            pickup_lat = data.get('pickup_lat')
            pickup_lng = data.get('pickup_lng')
            destination_lat = data.get('destination_lat')
            destination_lng = data.get('destination_lng')
            pickup_address = data.get('pickup_address', '')
            destination_address = data.get('destination_address', '')
            distance_km = data.get('distance_km')
            duration_min = data.get('duration_min')
            vehicle_type = data.get('vehicle_type')
            payment_method = data.get('payment_method', 'deferred')
            # Idempotency key for this booking. Optional, so older clients are
            # unaffected; length-bounded because it reaches a CharField(64).
            client_request_id = data.get('client_request_id')
            if client_request_id is not None:
                client_request_id = str(client_request_id).strip()[:64] or None

            # Validate required fields
            if not all([pickup_lat, pickup_lng, destination_lat, destination_lng, pickup_address, destination_address]):
                await self.send(text_data=json.dumps({
                    'type': 'error',
                    'message': 'pickup_lat, pickup_lng, destination_lat, destination_lng, pickup_address, destination_address are required'
                }))
                return

            # Create trip in database
            trip, reused = await self._create_trip(
                pickup_lat=pickup_lat,
                pickup_lng=pickup_lng,
                destination_lat=destination_lat,
                destination_lng=destination_lng,
                pickup_address=pickup_address,
                destination_address=destination_address,
                distance_km=distance_km,
                duration_min=duration_min,
                vehicle_type=vehicle_type,
                payment_method=payment_method,
                client_request_id=client_request_id,
            )

            if not trip:
                await self.send(text_data=json.dumps({
                    'type': 'error',
                    'message': 'Failed to create trip'
                }))
                return

            # Confirm trip creation to rider. `reused` is additive and tells an
            # app that its retry was recognised rather than duplicated.
            await self.send(text_data=json.dumps({
                'type': 'trip_created',
                'trip_id': trip.id,
                'estimated_fare': str(trip.estimated_fare) if trip.estimated_fare else None,
                'reused': reused,
                'message': ('Reconnected to your existing request...' if reused
                            else 'Searching for nearby drivers...'),
            }))

            if reused:
                # Everything below is a side effect of *booking*, and this request
                # already booked. Re-running it would publish a second stream
                # event and start a second dispatch chain competing for drivers on
                # the same trip -- the duplicate this key exists to prevent.
                return

            # Log ride request to Redis Stream (for future analytics, not driver notification)
            await self._publish_ride_request(trip)

            # Hand the driver search to Celery. This socket does not execute
            # waves and is not required for the search to complete — see
            # servers/ride/dispatch.py.
            started = await self._start_dispatch(trip.id, reason='initial')
            if not started.get('enqueued'):
                # There is exactly one dispatch engine, so we do not fall
                # back to running waves here. The trip stays committed and
                # its durable auto_cancel_trip deadline still applies.
                await self.send(text_data=json.dumps({
                    'type': 'dispatch_failed',
                    'trip_id': trip.id,
                    'message': (
                        'We could not start the driver search. Please retry '
                        'in a moment.'
                    ),
                }))

        except json.JSONDecodeError:
            await self.send(text_data=json.dumps({
                'type': 'error',
                'message': 'Invalid JSON'
            }))
        except Exception as e:
            logger.error(f"Error in RideRequestConsumer.receive: {str(e)}")
            await self.send(text_data=json.dumps({
                'type': 'error',
                'message': 'Internal server error'
            }))

    # -- Retry & shared helpers --

    async def _handle_retry(self, data):
        """Re-run the driver search for a trip that is still unassigned.

        Expected: {"action": "retry", "trip_id": int, "radius": int optional}

        Routed through the same Celery entry point as the initial search so
        there is exactly one dispatch execution architecture. This used to
        `await` the wave loop inline on the socket, which made retry
        socket-owned in the same way the initial search was.
        """
        trip_id = data.get('trip_id')
        radius = min(int(data.get('radius', 5000)), 5000)

        if not trip_id:
            await self.send(text_data=json.dumps({
                'type': 'error',
                'message': 'trip_id is required for retry'
            }))
            return

        trip = await self._get_pending_trip(trip_id)
        if not trip:
            await self.send(text_data=json.dumps({
                'type': 'error',
                'message': 'Trip not found or already accepted by a driver'
            }))
            return

        # A fresh generation. Its epoch scopes notification de-duplication so
        # drivers who ignored the previous search are reachable again, which
        # is what the old implementation did implicitly by starting each call
        # with an empty in-process `offered` set.
        started = await self._start_dispatch(trip.id, radius=radius, reason='retry')

        if not started.get('enqueued'):
            await self.send(text_data=json.dumps({
                'type': 'dispatch_failed',
                'trip_id': trip.id,
                'message': 'Could not restart the driver search. Please try again.',
            }))
            return

        await self.send(text_data=json.dumps({
            'type': 'retry_started',
            'trip_id': trip.id,
            'radius': radius,
            'message': 'Retrying — searching for nearby drivers...'
        }))

    @database_sync_to_async
    def _start_dispatch(self, trip_id, radius=None, reason='initial'):
        from servers.ride.dispatch import start_dispatch
        return start_dispatch(trip_id, radius=radius, reason=reason)

    @database_sync_to_async
    def _current_trip_snapshot(self):
        """Authoritative current-trip state for this rider, or None.

        Reuses TripDetailSerializer rather than inventing a second shape, so
        the OTP rule it already enforces — only the trip's own rider ever
        sees `otp` — continues to apply here, and the driver's phone number
        is never part of that representation.
        """
        from servers.ride.models import Trip
        from servers.ride.serializers import TripDetailSerializer

        ACTIVE = ('requested', 'accepted', 'reached', 'in_progress')
        trip = (
            Trip.objects
            .filter(user_id=self.user, status_id__status_code__in=ACTIVE)
            .select_related('status_id', 'driver_id__user_id', 'vehicle_id__vehicle_type_id', 'user_id')
            .prefetch_related('fare_pricing', 'ratings')
            .order_by('-requested_at')
            .first()
        )
        if trip is None:
            return None

        # Scoped to `user_id=self.user` above, so one rider can never be
        # handed another rider's trip. The serializer's OTP gate reads
        # request.user, so pass this rider through as the request identity.
        class _Ctx:
            user = self.user

        return TripDetailSerializer(trip, context={'request': _Ctx()}).data

    # -- Event handlers --

    async def dispatch_progress(self, event):
        """Search progress from a Celery dispatch wave.

        Replaces the `drivers_notified` / `no_drivers` frames the consumer
        used to emit while it owned the wave loop. Sent to the rider *group*
        by servers.ride.dispatch, so it reaches whichever socket the rider
        currently holds, or nobody at all if they are offline — dispatch does
        not depend on anyone receiving it.
        """
        await self.send(text_data=json.dumps({
            'type': 'dispatch_progress',
            'trip_id': event['trip_id'],
            'wave': event.get('wave'),
            'waves_total': event.get('waves_total'),
            'radius_m': event.get('radius_m'),
            'drivers_notified': event.get('drivers_notified', 0),
            'final_wave': event.get('final_wave', False),
        }))

    async def trip_update(self, event):
        """Send trip status update to rider."""
        response_data = {
            'type': 'trip_update',
            'trip_id': event['trip_id'],
            'status': event['status'],
            'message': event.get('message', ''),
            'driver_id': event.get('driver_id'),
            'driver_name': event.get('driver_name', ''),
        }
        
        # Add driver info and OTP if the ride was accepted
        if event['status'] == 'accept':
            if 'otp' in event:
                response_data['otp'] = event['otp']
            if 'driver_info' in event:
                response_data['driver_info'] = event['driver_info']
            if 'vehicle_info' in event:
                response_data['vehicle_info'] = event['vehicle_info']

        await self.send(text_data=json.dumps(response_data))

    async def driver_location_update(self, event):
        """Forward live driver location, without blocking the dispatch loop.

        A rider whose app is slow to drain must still be able to cancel.
        """
        self._queue_location_frame({
            'type': 'driver_location_update',
            'lng': event['lng'],
            'lat': event['lat'],
            'driver_id': event['driver_id'],
        })
        
    async def cash_payment_confirmed(self, event):
        """Notify rider that cash payment has been confirmed."""
        await self.send(text_data=json.dumps({
            'type': 'cash_payment_confirmed',
            'trip_id': event['trip_id'],
            'message': 'Cash payment has been confirmed by driver',
        }))

    # -- Database helpers --

    @database_sync_to_async
    def _create_trip(self, pickup_lat, pickup_lng, destination_lat, destination_lng,
                     pickup_address, destination_address,
                     distance_km=None, duration_min=None, vehicle_type=None,
                     payment_method='deferred', client_request_id=None):
        """Create the trip, or return the one this request already created.

        Returns `(trip, reused)`. `reused=True` means `client_request_id` had
        already produced a trip for this rider, so the caller must NOT re-run any
        of the side effects that follow a booking -- dispatch, the ride-request
        stream event, the surge demand record.

        The lookup deliberately happens before the fare is computed, because
        `estimate_amount(record_demand=True)` registers demand for surge pricing.
        A double-tap that got as far as pricing would push the rider's own surge
        multiplier up before quoting them.
        """
        from decimal import Decimal
        from servers.ride.models import Trip, FarePricing
        from servers.ride.utils import estimate_amount, validate_distance
        from servers.driver.models import VehicleType
        from django.db import IntegrityError, transaction

        if client_request_id:
            existing = Trip.objects.filter(
                user_id=self.user, client_request_id=client_request_id,
            ).first()
            if existing is not None:
                logger.info('trip_request_reused', extra={
                    'event': 'trip_request_reused',
                    'trip_id': existing.id,
                    'rider_id': self.user.id,
                })
                return existing, True

        try:
            # Parse distance and duration
            try:
                dist = float(distance_km) if distance_km is not None else 0
                dur = float(duration_min) if duration_min is not None else 0
            except (ValueError, TypeError):
                dist, dur = 0, 0

            # Service-area gate: reject the request before any fare work
            # if either coordinate is outside the Hyderabad polygon. Same
            # check the REST estimate-fare endpoint runs.
            from base.service_area import validate_service_area
            area_ok, area_msg = validate_service_area(
                pickup_lat, pickup_lng, destination_lat, destination_lng,
            )
            if not area_ok:
                logger.warning(f"Service area rejection: {area_msg}")
                raise ValueError(area_msg)

            is_valid, validated_km, validated_min, msg = validate_distance(
                dist, dur, pickup_lat, pickup_lng, destination_lat, destination_lng
            )
            if not is_valid:
                logger.warning(f"Distance validation failed: {msg}")
                raise ValueError(f"Invalid distance: {msg}")

            # Fare is computed from the SERVER-computed validated distance
            # and duration, not the client-supplied values. The client
            # numbers are advisory only (used to flag suspicious deviation
            # inside validate_distance).
            fare = estimate_amount(
                validated_km,
                validated_min,
                vehicle_type=vehicle_type,
                pickup_lat=pickup_lat,
                pickup_long=pickup_lng,
                rider_id=self.user.id,
                # This IS a booking, so it counts toward demand for surge.
                record_demand=True,
            )

            # Resolve VehicleType for storing on Trip
            requested_vt = None
            if vehicle_type:
                requested_vt = VehicleType.objects.filter(type__iexact=vehicle_type).first()

            # Stamp the zone the pickup fell in. Commission, GST and every
            # per-city report read this instead of re-running a
            # point-in-polygon lookup against a zone table that may have
            # changed since the ride happened.
            from servers.pricing.services import find_zone_for_point
            pickup_zone = find_zone_for_point(pickup_lat, pickup_lng)

            with transaction.atomic():
                trip = Trip.objects.create(
                    user_id=self.user,
                    client_request_id=client_request_id or None,
                    zone=pickup_zone,
                    pickup_lat=pickup_lat,
                    pickup_long=pickup_lng,
                    destination_lat=destination_lat,
                    destination_long=destination_lng,
                    pickup_address=pickup_address,
                    destination_address=destination_address,
                    estimated_fare=fare['total_fare'],
                    estimated_distance_km=Decimal(str(dist)) if dist else None,
                    estimated_duration_min=Decimal(str(dur)) if dur else None,
                    surge_multiplier=fare['surge_multiplier'],
                    requested_vehicle_type=requested_vt,
                    payment_method=payment_method,
                )

                FarePricing.objects.create(
                    trip_id=trip,
                    base_fare=fare['base_fare'],
                    distance_fare=fare['distance_fare'],
                    time_fare=fare['time_fare'],
                    surge_multiplier=fare['surge_multiplier'],
                    total_fare=fare['total_fare'],
                )

            # Cache new trip in Redis for fast state reads
            from servers.redis_client import cache_trip as _cache_trip
            _cache_trip(
                trip.id,
                status='requested',
                rider_id=str(self.user.id),
                pickup_lat=str(pickup_lat),
                pickup_lng=str(pickup_lng),
                destination_lat=str(destination_lat),
                destination_lng=str(destination_lng),
                estimated_fare=str(fare['total_fare']),
                payment_method=payment_method or 'cash',
            )

            # Schedule auto-cancel task.
            #
            # Guarded separately from trip creation, and deliberately so. The trip
            # is COMMITTED by the time this runs -- the atomic block above has
            # closed. Celery's broker is Redis, so an unreachable Redis made
            # apply_async raise, the broad `except Exception` below caught it, and
            # this method returned (None, False): its "creation failed" answer, for
            # a trip that exists. The rider was told their booking failed while a
            # `requested` row sat in PostgreSQL with no timeout scheduled, so
            # nothing would ever move it out of `requested`.
            #
            # Now the booking succeeds, because it did, and the enqueue failure is
            # reported as its own event.
            #
            # Residual risk, stated rather than hidden: a trip created while the
            # broker is unreachable has NO scheduled timeout. Dispatch needs Redis
            # too, so no driver is offered the trip either -- the rider sees a
            # booking that finds no drivers rather than a phantom, and an operator
            # can cancel it. The alternative, rolling the trip back from out here,
            # cannot be done after commit.
            from servers.ride.tasks import auto_cancel_trip
            from django.conf import settings
            try:
                auto_cancel_trip.apply_async(
                    (trip.id,), countdown=settings.TRIP_ACCEPT_TIMEOUT_SECONDS,
                )
            except Exception as enqueue_error:  # noqa: BLE001
                logger.error('trip_autocancel_enqueue_failed', extra={
                    'event': 'trip_autocancel_enqueue_failed',
                    'trip_id': trip.id,
                    'rider_id': self.user.id,
                    'error': type(enqueue_error).__name__,
                })

            return trip, False
        except IntegrityError:
            # Two identical requests raced and the other one won. The unique index
            # is the arbiter, so re-read rather than guess -- this is the branch a
            # Redis-only check could not make safe.
            if client_request_id:
                existing = Trip.objects.filter(
                    user_id=self.user, client_request_id=client_request_id,
                ).first()
                if existing is not None:
                    logger.info('trip_request_raced', extra={
                        'event': 'trip_request_raced',
                        'trip_id': existing.id,
                        'rider_id': self.user.id,
                    })
                    return existing, True
            logger.exception('Failed to create trip (integrity)')
            return None, False
        except Exception as e:
            logger.error(f"Failed to create trip: {str(e)}")
            return None, False

    @database_sync_to_async
    def _find_nearby_drivers(self, lng, lat, vehicle_type=None, radius=5000):
        from servers.redis_client import nearby_drivers
        return nearby_drivers(
            lng=lng, lat=lat, vehicle_type=vehicle_type, radius=radius,
        )

    @database_sync_to_async
    def _record_offered(self, trip_id, driver_ids):
        from servers.redis_client import add_offered_drivers
        return add_offered_drivers(trip_id, driver_ids)

    @database_sync_to_async
    def _trip_still_open(self, trip_id):
        """True while the trip is unassigned and still in 'requested'."""
        from servers.ride.models import Trip
        try:
            trip = Trip.objects.select_related('status_id').only(
                'id', 'driver_id', 'status_id',
            ).get(id=trip_id)
        except Trip.DoesNotExist:
            return False
        if trip.driver_id_id:
            return False
        return (trip.status_id.status_code if trip.status_id else None) == 'requested'

    @database_sync_to_async
    def _publish_ride_request(self, trip):
        from servers.redis_client import publish_ride_request
        return publish_ride_request(
            ride_id=trip.id,
            rider_id=self.user.id,
            pickup_lng=float(trip.pickup_long),
            pickup_lat=float(trip.pickup_lat),
            destination_lng=float(trip.destination_long),
            destination_lat=float(trip.destination_lat),
        )

    @database_sync_to_async
    def _get_rider_name(self):
        try:
            return self.user.full_name or self.user.phone_number
        except Exception:
            return ''

    @database_sync_to_async
    def _get_pending_trip(self, trip_id):
        """Get a trip only if it belongs to this rider and has no driver assigned yet."""
        from servers.ride.models import Trip
        try:
            trip = Trip.objects.get(id=trip_id, user_id=self.user)
            if trip.driver_id is not None:
                return None
            return trip
        except Trip.DoesNotExist:
            return None

    @database_sync_to_async
    def _send_driver_push(self, driver_id, title, body, data):
        from servers.driver.models import Driver
        from servers.auth_user.services import send_push_notification
        try:
            driver = Driver.objects.select_related('user_id').get(id=driver_id)
            send_push_notification(driver.user_id, title, body, data)
        except Exception as e:
            logger.error(f"Failed to send push to driver {driver_id}: {str(e)}")


class TripStatusConsumer(LocationBroadcastMixin, AsyncWebsocketConsumer):
    """
    WebSocket consumer for real-time trip status updates.
    Both rider and driver join a trip-specific group.
    
    Connect: ws://host/ws/ride/trip/<trip_id>/?token=<jwt>
    Send (driver only):
        {"action": "accept"}
        {"action": "reached"}
        {"action": "start"}
        {"action": "complete"}
        {"action": "cancel"}
    """

    async def connect(self):
        self.user = self.scope.get('user', AnonymousUser())

        if isinstance(self.user, AnonymousUser) or not self.user.is_authenticated:
            await self.close(code=4001)
            return

        self.trip_id = self.scope['url_route']['kwargs']['trip_id']
        self.trip_group = f'trip_{self.trip_id}'
        self.in_trip_group = False

        # Resolve *how* this user relates to the trip. Only the rider and
        # the ASSIGNED driver may join the trip group — a candidate driver
        # who has been offered the ride connects but stays out of the group
        # until their accept transaction commits.
        #
        # Before this gate, any approved driver could open
        # /ws/ride/trip/<id>/ for an unassigned trip and then keep
        # receiving trip_status_update + driver_location_update for the
        # whole ride even after losing the race — leaking the rider's
        # pickup/drop and the winning driver's live GPS track.
        self.participation = await self._resolve_participation()
        if self.participation == 'none':
            await self.close(code=4003)
            return

        if self.participation in ('rider', 'assigned_driver'):
            await self.channel_layer.group_add(self.trip_group, self.channel_name)
            self.in_trip_group = True

        await self.accept()
        # Reconnect recovery. A client that lost its acknowledgement -- or its
        # socket -- must be able to discover durable truth immediately rather than
        # infer it, so the greeting carries the committed status. Read from
        # PostgreSQL, never the Redis cache: a stale cache is precisely how a
        # completed ride would look unfinished.
        current_status = None
        try:
            current_status = await self._current_trip_status()
        except Exception:  # noqa: BLE001 -- a greeting must not fail the connect
            logger.exception('trip_status_on_connect_failed')
        await self.send(text_data=json.dumps({
            'type': 'connection_established',
            'trip_id': self.trip_id,
            'subscribed': self.in_trip_group,
            'trip_status': current_status,
            'message': 'Connected to trip updates',
        }))
        await self._start_location_pump()

    async def disconnect(self, close_code):
        await self._stop_location_pump()
        if getattr(self, 'in_trip_group', False):
            await self.channel_layer.group_discard(self.trip_group, self.channel_name)
            self.in_trip_group = False

        # If this socket goes away while the ride is still running, somebody just
        # lost the channel they issue `complete`, `cancel` and `start` on -- and
        # their app will not necessarily know, because a send() on a half-open
        # socket succeeds. That is exactly how a completed ride was left
        # `in_progress` with nothing logged anywhere.
        #
        # Emitted at WARNING so it survives production's log level. A normal ride
        # ends with the trip already terminal, so this stays quiet in the good case.
        try:
            status = await self._current_trip_status()
        except Exception:  # noqa: BLE001 -- observability must never break teardown
            return
        if status is not None and status not in ('completed', 'cancelled'):
            logger.warning('trip_command_socket_lost', extra={
                'event': 'trip_command_socket_lost',
                'trip_id': getattr(self, 'trip_id', None),
                'participation': getattr(self, 'participation', None),
                'trip_status': status,
                'close_code': close_code,
            })

    @database_sync_to_async
    def _current_trip_status(self):
        """The trip's status code, or None if it has gone."""
        from servers.ride.models import Trip

        return (Trip.objects.filter(id=self.trip_id)
                .values_list('status_id__status_code', flat=True).first())


    # -- Application-level command acknowledgement --
    #
    # A successful `send()` on a WebSocket proves only that bytes left the client.
    # It does not prove the command reached the server, and it certainly does not
    # prove anything committed -- a resolved pilot blocker turned on exactly that
    # distinction, where a driver app reported a finished ride whose trip was still
    # `in_progress`.
    #
    # Worse, and reachable with no transport failure at all: before this the ONLY
    # success signal for every lifecycle command was the `trip_status_update`
    # broadcast, sent with `channel_layer.group_send`. channels_redis *silently
    # drops* messages to a channel that is over capacity (default 100), logging
    # only "N of M channels over capacity in group G". So a committed completion
    # could go unacknowledged by design, while the driver app sat waiting for a
    # frame that had already been discarded.
    #
    # `command_ack` is a DIRECT send to the socket that issued the command, emitted
    # only after the database transaction has returned. It is purely additive:
    # `trip_status_update` and `error` are unchanged, so a client that has never
    # heard of `command_ack` behaves exactly as it did before.

    ACK_COMMITTED = 'committed'         # this call performed the transition
    ACK_ALREADY_DONE = 'already_done'   # durable state already satisfies it
    ACK_REJECTED = 'rejected'           # refused; nothing changed

    async def _ack(self, command, status, command_id=None, trip_status=None,
                   reason=None, detail=None):
        """Acknowledge one command, directly and after the fact.

        `command_id` is echoed straight back so the client can match a reply to
        the command it sent. It is client-generated and opaque to us; the server
        never invents one, because a correlation id the client did not choose
        correlates nothing.
        """
        frame = {
            'type': 'command_ack',
            'command': command,
            'status': status,
            'trip_id': self.trip_id,
        }
        if command_id is not None:
            frame['command_id'] = command_id
        if trip_status is not None:
            frame['trip_status'] = trip_status
        if reason:
            frame['reason'] = reason
        if detail:
            frame['detail'] = detail
        await self.send(text_data=json.dumps(frame))

    async def _ack_rejected(self, command, command_id, reason, detail=None,
                            trip_status=None):
        """Refusals that never reach the state machine still deserve an ack.

        An authorisation failure, a missing OTP, an exhausted attempt lock: from
        the client's point of view these are answers, and a client waiting for one
        must not time out and retry a command that will never be accepted.
        """
        await self._ack(command, self.ACK_REJECTED, command_id=command_id,
                        reason=reason, detail=detail, trip_status=trip_status)

    async def receive(self, text_data):
        """
        Receive trip actions from driver.
        Expected: {"action": "accept|reached|start|complete|cancel",
                   "command_id": "<optional client-generated correlation id>"}
        """
        try:
            data = json.loads(text_data)
            action = data.get('action')
            otp_input = data.get('otp')
            # Opaque, client-generated, echoed verbatim. Length-bounded so it
            # cannot be used to push arbitrary payload back out through us.
            command_id = data.get('command_id')
            if command_id is not None:
                command_id = str(command_id)[:64]

            if action == 'ping':
                # The driver app keeps this socket warm with a 25-second
                # application-level ping, because a client cannot send a
                # protocol-level ping frame from Dart. Before this it used an
                # unknown action and relied on the `Invalid action` error frame as
                # its liveness proof -- which worked, but meant the socket that
                # carries `complete` also carried a steady stream of error frames,
                # indistinguishable from a real command failure.
                await self.send(text_data=json.dumps({'type': 'pong'}))
                return

            if action not in ('accept', 'reached', 'start', 'complete', 'cancel', 'confirm_cash'):
                await self.send(text_data=json.dumps({
                    'type': 'error',
                    'message': 'Invalid action. Must be: accept, reached, start, complete, cancel, confirm_cash, or ping'
                }))
                await self._ack_rejected(str(action)[:32], command_id,
                                         'unknown_command')
                return

            # Authorisation ladder:
            #  - accept:    any logged-in driver (the race to assignment is
            #               handled by select_for_update in _accept_trip).
            #  - reached / start / complete / confirm_cash:
            #               must be the ASSIGNED driver of THIS trip, not
            #               just any driver who happens to be subscribed
            #               to the trip group.
            #  - cancel:    must be the assigned driver OR the trip's rider.
            #
            # Previously every post-accept action only checked "is this user
            # A driver", and `cancel` had no role check at all — meaning any
            # logged-in user subscribed to /ws/ride/trip/<id>/ could cancel
            # an arbitrary trip.

            if action == 'accept':
                if self.participation not in ('candidate_driver', 'assigned_driver'):
                    await self.send(text_data=json.dumps({
                        'type': 'error',
                        'message': 'Only drivers can accept rides'
                    }))
                    await self._ack_rejected(action, command_id,
                                             'not_a_driver')
                    return
                result = await self._accept_trip()
                if result.get('success'):
                    # Subscription happens only AFTER the accept transaction
                    # commits and Trip.driver_id points at this driver.
                    if not self.in_trip_group:
                        await self.channel_layer.group_add(
                            self.trip_group, self.channel_name,
                        )
                        self.in_trip_group = True
                    self.participation = 'assigned_driver'
                    # Losing candidates were already dismissed inside
                    # _accept_trip via dismiss_outstanding_offers(), which
                    # owns that fanout for every terminal transition.
                elif result.get('taken'):
                    # Lost the race. Tell the app to dismiss the request card
                    # and drop the socket so it cannot observe the trip.
                    await self.send(text_data=json.dumps({
                        'type': 'trip_taken',
                        'trip_id': self.trip_id,
                        'message': 'This ride was accepted by another driver.',
                    }))
                    await self._ack_rejected(action, command_id, 'trip_taken')
                    await self.close(code=4005)
                    return
            elif action == 'reached':
                if not await self._is_assigned_driver():
                    await self.send(text_data=json.dumps({
                        'type': 'error',
                        'message': 'Only the assigned driver can mark this trip as reached'
                    }))
                    await self._ack_rejected(action, command_id,
                                             'not_assigned_driver')
                    return
                result = await self._update_trip_status('reached')
            elif action == 'start':
                if not await self._is_assigned_driver():
                    await self.send(text_data=json.dumps({
                        'type': 'error',
                        'message': 'Only the assigned driver can start this trip'
                    }))
                    await self._ack_rejected(action, command_id,
                                             'not_assigned_driver')
                    return
                if not otp_input:
                    await self.send(text_data=json.dumps({
                        'type': 'error',
                        'message': 'OTP is required to start the ride'
                    }))
                    await self._ack_rejected(action, command_id,
                                             'otp_required')
                    return
                # Brute-force lock: refuse start if too many wrong OTPs have
                # been submitted for this trip recently.
                attempt_key = f"trip_otp_attempts:{self.trip_id}"
                attempts = cache.get(attempt_key, 0)
                if attempts >= TRIP_OTP_MAX_ATTEMPTS:
                    await self.send(text_data=json.dumps({
                        'type': 'error',
                        'message': 'Too many invalid OTP attempts. Ask the rider for the OTP again.'
                    }))
                    await self._ack_rejected(action, command_id,
                                             'otp_attempts_exhausted')
                    return
                result = await self._update_trip_status('in_progress', otp_input=otp_input)
                if not result.get('success') and 'OTP' in (result.get('error') or ''):
                    # Increment attempt counter on OTP failure
                    try:
                        cache.set(attempt_key, attempts + 1, TRIP_OTP_ATTEMPT_TTL_SECONDS)
                    except Exception:
                        pass
                elif result.get('success'):
                    cache.delete(attempt_key)
            elif action == 'complete':
                if not await self._is_assigned_driver():
                    await self.send(text_data=json.dumps({
                        'type': 'error',
                        'message': 'Only the assigned driver can complete this trip'
                    }))
                    await self._ack_rejected(action, command_id,
                                             'not_assigned_driver')
                    return
                result = await self._update_trip_status('completed')
            elif action == 'confirm_cash':
                if not await self._is_assigned_driver():
                    await self.send(text_data=json.dumps({
                        'type': 'error',
                        'message': 'Only the assigned driver can confirm cash payment'
                    }))
                    await self._ack_rejected(action, command_id,
                                             'not_assigned_driver')
                    return
                result = await self._confirm_cash_payment()
            elif action == 'cancel':
                is_rider = await self._is_rider()
                is_assigned = await self._is_assigned_driver()
                if not (is_rider or is_assigned):
                    await self.send(text_data=json.dumps({
                        'type': 'error',
                        'message': 'Only the rider or assigned driver can cancel this trip'
                    }))
                    await self._ack_rejected(action, command_id,
                                             'not_authorised_to_cancel')
                    return
                # Record WHO cancelled. Ops, refunds, driver-penalty and the
                # MVA-2020 cancellation policy all need this; collapsing every
                # cancel into a single `cancelled` status loses it.
                result = await self._update_trip_status(
                    'cancelled',
                    cancelled_by='rider' if is_rider else 'driver',
                    cancellation_reason=str(data.get('reason', ''))[:255],
                )

            if result.get('success'):
                # Broadcast status to all participants in the trip group
                status_event = {
                    'type': 'trip_status_update',
                    'trip_id': self.trip_id,
                    'status': action,
                    'message': result.get('message', ''),
                    'driver_id': result.get('driver_id'),
                }
                
                if action == 'accept':
                    status_event.update({
                        'otp': result.get('otp'),
                        'driver_info': result.get('driver_info'),
                        'vehicle_info': result.get('vehicle_info'),
                    })

                await self.channel_layer.group_send(self.trip_group, status_event)

                # Also notify the rider via their personal group
                rider_id = result.get('rider_id')
                if rider_id:
                    rider_event = {
                        'type': 'trip_update',
                        'trip_id': self.trip_id,
                        'status': action,
                        'message': result.get('message', ''),
                        'driver_id': result.get('driver_id'),
                        'driver_name': result.get('driver_name', ''),
                    }
                    
                    if action == 'accept':
                        rider_event.update({
                            'otp': result.get('otp'),
                            'driver_info': result.get('driver_info'),
                            'vehicle_info': result.get('vehicle_info'),
                        })

                    await self.channel_layer.group_send(f'rider_{rider_id}', rider_event)

                # Direct, post-commit, correlated. `_update_trip_status` and
                # `_accept_trip` both wrap `transaction.atomic()`, so by the time
                # they have returned the write is durable -- which is what lets
                # this ack mean "committed" rather than "parsed".
                # For a command that moves the state machine, the target status is
                # known. For one that does not (`confirm_cash`), read the trip's
                # real status rather than inventing one -- an ack that reports a
                # status the trip is not in is worse than no ack.
                acked_status = _TARGET_STATUS.get(action)
                if acked_status is None:
                    try:
                        acked_status = await self._current_trip_status()
                    except Exception:  # noqa: BLE001 -- never fail the ack on this
                        acked_status = None
                await self._ack(action, self.ACK_COMMITTED,
                                command_id=command_id,
                                trip_status=acked_status)
            else:
                # Sent only to the acting driver's own socket, never to the
                # trip group, so a rejection cannot disclose anything about
                # another rider's trip. `reason` is a stable machine-readable
                # code the driver app can branch on (e.g.
                # 'driver_already_on_trip'); it is additive, so clients that
                # only read `message` are unaffected.
                reason = result.get('reason')

                if reason == 'already_in_target_state':
                    # A retry that landed after the original attempt committed.
                    # The durable state already satisfies the command, so this is
                    # a SUCCESS for the caller, and nothing ran twice. Replying
                    # with an `error` frame here is exactly what would make a
                    # correct retry look like a failure.
                    await self._ack(action, self.ACK_ALREADY_DONE,
                                    command_id=command_id,
                                    trip_status=result.get('current_status'),
                                    reason=reason)
                else:
                    error_frame = {
                        'type': 'error',
                        'message': result.get('error', 'Action failed'),
                    }
                    if reason:
                        error_frame['reason'] = reason
                    await self.send(text_data=json.dumps(error_frame))
                    await self._ack_rejected(
                        action, command_id, reason or 'rejected',
                        detail=result.get('error'),
                        trip_status=result.get('current_status'),
                    )

        except json.JSONDecodeError:
            await self.send(text_data=json.dumps({
                'type': 'error',
                'message': 'Invalid JSON'
            }))
        except Exception as e:
            logger.error(f"Error in TripStatusConsumer.receive: {str(e)}")
            await self.send(text_data=json.dumps({
                'type': 'error',
                'message': 'Internal server error'
            }))

    # -- Event handlers --

    async def trip_status_update(self, event):
        """Broadcast trip status to all participants."""
        await self.send(text_data=json.dumps({
            'type': 'trip_status_update',
            'trip_id': event['trip_id'],
            'status': event['status'],
            'message': event.get('message', ''),
            'driver_id': event.get('driver_id'),
        }))

    async def driver_location_update(self, event):
        """Forward live driver location to the RIDER on this trip.

        Not to the driver. `group_send('trip_<id>', ...)` reaches the whole trip
        group, so before this the assigned driver received an echo of every
        position it had just sent -- one frame per GPS ping, on the same socket
        that carries `complete`, `cancel` and `start`, for no purpose at all. The
        driver is the source of that position.

        That echo was the whole failure. A driver app that is slow to drain fills
        its receive buffer with its own GPS echo; the reference `websockets` client
        stops reading frames at sixteen queued messages, **including ping frames**,
        so it stops answering Daphne's keepalive; Daphne's ping timeout elapses and
        the server closes the connection. The app never notices -- its `send()`
        still succeeds -- so `complete` goes into a dead socket and the trip stays
        `in_progress` while the driver is told the ride is over.

        Observed in QA exactly as this predicts: with thirty pings ten seconds
        apart, the buffer filled at ping sixteen and the server logged
        WSDISCONNECT about fifty seconds later, eighty-seven seconds before
        `complete` was sent.

        Queued rather than sent for the rider's sake, so a slow rider app cannot
        block the loop either. See LocationBroadcastMixin.
        """
        if getattr(self, 'participation', None) == 'assigned_driver':
            return

        self._queue_location_frame({
            'type': 'driver_location_update',
            'lng': event['lng'],
            'lat': event['lat'],
            'driver_id': event['driver_id'],
        })
        
    async def cash_payment_confirmed(self, event):
        """Notify rider that cash payment has been confirmed."""
        await self.send(text_data=json.dumps({
            'type': 'cash_payment_confirmed',
            'trip_id': event['trip_id'],
            'message': 'Cash payment has been confirmed by driver',
        }))

    # -- Database helpers --

    @database_sync_to_async
    def _resolve_participation(self):
        """Classify this user's relationship to the trip.

        Returns one of:
          'rider'            -- the rider who booked it
          'assigned_driver'  -- the driver currently assigned to it
          'candidate_driver' -- an approved driver, trip still unassigned;
                                may send {"action": "accept"} but is NOT
                                subscribed to the trip group
          'none'             -- reject the connection
        """
        from servers.ride.models import Trip
        try:
            trip = Trip.objects.select_related('status_id').get(id=self.trip_id)
        except Trip.DoesNotExist:
            return 'none'

        if trip.user_id_id == self.user.id:
            return 'rider'

        driver = getattr(self.user, 'driver', None)
        if driver is None:
            return 'none'

        if trip.driver_id_id:
            return 'assigned_driver' if trip.driver_id_id == driver.id else 'none'

        # Unassigned trip: only an approved, unblocked driver may bid on it,
        # and only while it is still open for acceptance.
        current = trip.status_id.status_code if trip.status_id else None
        if current not in (None, 'requested'):
            return 'none'
        if not driver.approved or (driver.status or '').strip().lower() == 'blocked':
            return 'none'
        return 'candidate_driver'

    @database_sync_to_async
    def _is_driver(self):
        try:
            return hasattr(self.user, 'driver') and self.user.driver is not None
        except Exception:
            return False

    @database_sync_to_async
    def _is_assigned_driver(self):
        """True only if self.user is THE assigned driver for this trip.

        Used to gate post-acceptance actions (reached/start/complete/cancel)
        — any other driver who managed to subscribe to /ws/ride/trip/<id>/
        must NOT be able to advance the trip state.
        """
        from servers.ride.models import Trip
        try:
            trip = Trip.objects.select_related('driver_id').get(id=self.trip_id)
            if not trip.driver_id or not hasattr(self.user, 'driver'):
                return False
            return trip.driver_id_id == self.user.driver.id
        except Trip.DoesNotExist:
            return False

    @database_sync_to_async
    def _is_rider(self):
        """True only if self.user is the rider for this trip."""
        from servers.ride.models import Trip
        try:
            trip = Trip.objects.get(id=self.trip_id)
            return trip.user_id_id == self.user.id
        except Trip.DoesNotExist:
            return False

    @database_sync_to_async
    def _accept_trip(self):
        from servers.ride.models import Trip, TripStatus
        from servers.driver.models import Driver
        from django.utils import timezone
        from django.db import transaction

        try:
            with transaction.atomic():
                trip = Trip.objects.select_for_update(of=('self',)).get(id=self.trip_id)

                # Check if already accepted
                if trip.driver_id is not None:
                    return {
                        'success': False,
                        'taken': True,
                        'error': 'Trip already accepted by another driver',
                    }

                # A trip that timed out or was cancelled while the offer was
                # on screen must not be acceptable.
                current = trip.status_id.status_code if trip.status_id else None
                if current not in (None, 'requested'):
                    return {
                        'success': False,
                        'taken': True,
                        'error': f'This ride is no longer available ({current}).',
                    }

                # Re-validate the driver's approval state INSIDE the lock.
                # The WS connect-time check is stale: an admin may have
                # revoked approval since this socket was opened, or the
                # driver's status may have flipped to 'blocked' (e.g. by
                # an expiry sweeper). Without this re-check, an unapproved
                # or blocked driver holding an old token could still take
                # rides.
                try:
                    driver = (
                        Driver.objects.select_for_update(of=('self',))
                        .select_related('user_id')
                        .get(pk=self.user.driver.id)
                    )
                except Driver.DoesNotExist:
                    return {'success': False, 'error': 'Driver profile not found'}

                if not driver.approved:
                    return {
                        'success': False,
                        'error': 'Your driver profile is not approved yet. '
                                 'Contact support if you believe this is in error.',
                    }
                if (driver.status or '').strip().lower() == 'blocked':
                    return {
                        'success': False,
                        'error': 'Your driver account is blocked.',
                    }
                if not driver.active_vehicle:
                    return {
                        'success': False,
                        'error': 'No active vehicle on file. Add or activate '
                                 'a vehicle before accepting rides.',
                    }

                # MVA 2020 fatigue gate. A driver who has done 12h in
                # the last 24h, OR is in a cancellation lockout, cannot
                # accept a new trip. Status is computed against
                # DriverSession + Driver.fatigue_lockout_until and
                # stamps the lockout column on the first breach so
                # subsequent checks are O(1).
                from servers.driver.fatigue import get_fatigue_status
                fatigue = get_fatigue_status(driver)
                if fatigue.locked:
                    return {
                        'success': False,
                        'error': (
                            'You have reached the 12-hour daily limit, or '
                            'are in a cooldown after recent cancellations. '
                            f'Try again after {fatigue.locked_until.isoformat() if fatigue.locked_until else "the cooldown ends"}.'
                        ),
                        'fatigue': fatigue.to_dict(),
                    }

                # One driver, one ride. PostgreSQL is the authority.
                #
                # Until now the only thing stopping a driver taking two rides
                # was Redis: `set_driver_active_trip` + `remove_driver` keep a
                # busy driver out of the geo index, so dispatch stops offering
                # to them. But that is an OFFER-time filter, and this function
                # — the single place a driver is ever assigned — never
                # consulted it. Three ways that failed:
                #
                #   * Redis loses the active-trip key (flush, restart,
                #     eviction). The next location ping re-adds the driver to
                #     the geo index and they are offered a second ride.
                #   * `set_driver_active_trip` fails at accept time and its
                #     False return is discarded, so the key is never written.
                #   * No infrastructure failure at all: two offers are already
                #     on the driver's screen from overlapping dispatch windows
                #     and they tap both.
                #
                # This read is authoritative because of the lock we are
                # already holding. Both competing transactions lock their own
                # Trip row first (different rows, no contention) and then
                # contend on this driver's row. The winner commits and
                # releases it; the loser acquires it afterwards and, under
                # READ COMMITTED, sees the committed assignment here.
                #
                # `exclude_trip_id` keeps a duplicate accept of THIS trip from
                # looking like a conflict with itself.
                from servers.ride.models import driver_active_trip_ids
                conflicting = driver_active_trip_ids(driver, exclude_trip_id=trip.id)
                if conflicting:
                    logger.warning(
                        'accept rejected: driver %s already on active trip(s) %s, '
                        'refused trip %s', driver.id, conflicting, trip.id,
                    )
                    # No Trip mutation, no OTP, no Redis write, no
                    # notifications — we return before any of that. The trip
                    # stays `requested` with no driver and remains available
                    # to other drivers.
                    return {
                        'success': False,
                        'reason': 'driver_already_on_trip',
                        'error': (
                            'You are already on an active ride. Finish or '
                            'cancel it before accepting another.'
                        ),
                    }

                status_obj, _ = TripStatus.objects.get_or_create(
                    status_code='accepted',
                    defaults={'description': 'Trip accepted by driver'}
                )

                trip.driver_id = driver
                trip.status_id = status_obj
                trip.accepted_at = timezone.now()

                # Generate and save OTP for the trip
                otp = generate_otp(6)
                trip.otp = otp
                trip.save()

            # Mark driver as busy in Redis so they don't get new ride requests
            from servers.redis_client import set_driver_active_trip, remove_driver
            set_driver_active_trip(driver.id, trip.id)
            remove_driver(driver.id)  # Remove from nearby drivers pool

            # Every other driver who was offered this ride needs an explicit
            # dismissal, otherwise their request card sits on screen until it
            # times out and they tap a dead trip. Shared with every other
            # terminal transition (rider/driver cancel, timeout) so the
            # behaviour exists once — see servers.ride.dispatch.
            from servers.ride.dispatch import dismiss_outstanding_offers
            losers = dismiss_outstanding_offers(
                trip.id, reason='accepted', exclude_driver_id=driver.id,
            )

            # Update ride cache with accepted status and driver assignment
            from servers.redis_client import cache_trip as _cache_trip
            vehicle = driver.active_vehicle
            _cache_trip(
                trip.id,
                status='accepted',
                driver_id=str(driver.id),
                driver_name=driver.user_id.full_name,
                driver_phone=driver.user_id.phone_number,
                driver_rating=str(driver.ratings),
                vehicle_model=vehicle.model if vehicle else 'Unknown',
                vehicle_brand=vehicle.brand if vehicle else 'Unknown',
                vehicle_number=vehicle.vehicle_number if vehicle else 'Unknown',
                vehicle_color=vehicle.color if vehicle else 'Unknown',
                otp=otp,
            )

            # Create notification for rider
            from servers.rider.models import Notification
            Notification.objects.create(
                user_id=trip.user_id,
                title='Ride Accepted',
                message=f'Driver {driver.user_id.full_name} has accepted your ride.',
            )
            
            from servers.auth_user.services import send_push_notification
            send_push_notification(
                trip.user_id, 
                "Ride Accepted", 
                f"Driver {driver.user_id.full_name} has accepted your ride.",
                {"trip_id": str(trip.id), "type": "ride_accepted"}
            )

            # Prepare driver and vehicle info for response
            driver_info = {
                'name': driver.user_id.full_name,
                'id': driver.id,
                'phone_number': driver.user_id.phone_number,
                'stars': str(driver.ratings),
            }
            
            vehicle = driver.active_vehicle
            vehicle_info = {
                'model': vehicle.model if vehicle else 'Unknown',
                'brand': vehicle.brand if vehicle else 'Unknown',
                'vehicle_number': vehicle.vehicle_number if vehicle else 'Unknown',
                'color': vehicle.color if vehicle else 'Unknown',
            }

            return {
                'success': True,
                'message': 'Trip accepted',
                'driver_id': driver.id,
                'driver_name': str(driver),
                'rider_id': trip.user_id_id,
                'otp': trip.otp,
                'driver_info': driver_info,
                'vehicle_info': vehicle_info,
                'notify_losers': losers,
            }
        except Trip.DoesNotExist:

            return {'success': False, 'error': 'Trip not found'}
        except Exception as e:
            logger.error(f"Error accepting trip: {str(e)}")
            return {'success': False, 'error': str(e)}

    @database_sync_to_async
    def _confirm_cash_payment(self):
        from servers.payments.models import Payment, TransactionHistory
        from servers.ride.models import Trip
        from django.db import transaction
        
        try:
            with transaction.atomic():
                trip = (
                    Trip.objects.select_for_update(of=('self',))
                    .select_related('status_id')
                    .get(id=self.trip_id)
                )

                # Cash can only be collected on a finished trip. Without this
                # gate the assigned driver could mark the fare collected while
                # the rider was still in the car (or before pickup).
                current = trip.status_id.status_code if trip.status_id else None
                if current != 'completed':
                    return {
                        'success': False,
                        'error': f'Cash can only be confirmed on a completed trip (currently {current}).',
                    }

                payment = trip.payments.filter(method='cash').first()
                if not payment:
                    return {'success': False, 'error': 'No cash payment found for this trip'}

                # Idempotent: a retried WS frame must not double-count GMV.
                if payment.status == 'completed':
                    return {
                        'success': True,
                        'message': 'Cash payment already confirmed',
                        'rider_id': trip.user_id_id,
                    }

                payment.status = 'completed'
                payment.save(update_fields=['status'])

                trip.payment_status = 'completed'
                trip.save(update_fields=['payment_status'])

                # NOTE: no TransactionHistory row is written here. The cash
                # branch of _create_payment_on_complete already wrote one at
                # completion time; writing a second one here double-counted
                # every cash trip in GMV and commission reporting.
                #
                # credit_driver_wallet is idempotent on
                # WalletTransaction.idempotency_key, so calling it again is a
                # no-op if the trip was already settled.
                from servers.driver.utils import credit_driver_wallet
                credit_driver_wallet(trip)

            return {'success': True, 'message': 'Cash payment confirmed', 'rider_id': trip.user_id_id}
        except Trip.DoesNotExist:
            return {'success': False, 'error': 'Trip not found'}
        except Exception as e:
            logger.error(f"Error confirming cash payment: {str(e)}", exc_info=True)
            return {'success': False, 'error': str(e)}
            
    @database_sync_to_async
    def _update_trip_status(self, status_code, otp_input=None,
                            cancelled_by='', cancellation_reason=''):
        """Advance the trip state machine.

        Everything inside the `atomic()` block is DB-only. Push
        notifications, receipt generation (PDF + S3 + email), Redis cache
        writes and any gateway call are registered with
        `transaction.on_commit` so that:

          * the Trip row lock is never held across a network round-trip
            (trip-completion latency used to include a reportlab render,
            an S3 PUT and an SES send), and
          * a rolled-back transaction can never fire a push, mail a
            receipt, or leave a gateway order behind.
        """
        from servers.ride.models import Trip, TripStatus
        from django.utils import timezone
        from django.db import transaction

        try:
            with transaction.atomic():
                # select_for_update() locks the row until the transaction ends
                trip = Trip.objects.select_for_update(of=('self',)).get(id=self.trip_id)
                current_status = trip.status_id.status_code if trip.status_id else None

                # Define strict transition rules
                allowed_transitions = {
                    'reached': ['accepted'],
                    'in_progress': ['accepted', 'reached'],
                    'completed': ['in_progress'],
                    # 'requested' added so the rider can cancel a trip that
                    # has not yet been accepted by any driver.
                    'cancelled': ['requested', 'accepted', 'reached', 'in_progress'],
                }

                # Enforcement of status transitions
                if status_code in allowed_transitions:
                    if current_status not in allowed_transitions[status_code]:
                        return {
                            'success': False, 
                            'current_status': current_status,
                            # `already_in_target_state` is the retry case and the
                            # whole point of these codes: a driver whose ack was
                            # lost retries, and must be told the command already
                            # succeeded rather than that it failed. No transition
                            # runs twice either way.
                            'reason': ('already_in_target_state'
                                       if current_status == status_code
                                       else 'invalid_transition'),
                            'error': f'Invalid status transition: cannot change from {current_status} to {status_code}'
                        }

                if status_code == 'cancelled' and current_status in ['completed', 'cancelled']:
                    return {
                        'success': False,
                        'current_status': current_status,
                        'reason': ('already_in_target_state'
                                   if current_status == 'cancelled'
                                   else 'trip_already_completed'),
                        'error': f'Trip is already {current_status}',
                    }

                if status_code == 'in_progress':
                    if str(trip.otp) != str(otp_input):
                        return {
                            'success': False,
                            'current_status': current_status,
                            'reason': 'invalid_otp',
                            'error': 'Invalid OTP provided'
                        }
                        
                status_obj, _ = TripStatus.objects.get_or_create(
                    status_code=status_code,
                    defaults={'description': f'Trip {status_code}'}
                )

                trip.status_id = status_obj

                from servers.auth_user.services import send_push_notification
                from servers.rider.models import Notification

                push = None  # (title, body, data) queued for after commit

                # Set timestamps based on status
                if status_code == 'reached':
                    trip.reached_at = timezone.now()
                    push = (
                        "Driver Arrived",
                        "Your driver has arrived at the pickup location.",
                        {"trip_id": str(trip.id), "type": "driver_arrived"},
                    )
                elif status_code == 'in_progress':
                    trip.started_at = timezone.now()
                    push = (
                        "Ride Started",
                        "Your ride is now in progress.",
                        {"trip_id": str(trip.id), "type": "ride_started"},
                    )
                elif status_code == 'completed':
                    trip.completed_at = timezone.now()
                    # Create payment on trip completion within the same transaction
                    self._create_payment_on_complete(trip)

                    fare = trip.final_fare or trip.estimated_fare
                    Notification.objects.create(
                        user_id=trip.user_id,
                        title='Ride Completed',
                        message=f'Your ride has been completed. Final fare: Rs.{fare}',
                    )
                    push = (
                        "Ride Completed",
                        f"Your ride has been completed. Final fare: Rs.{fare}",
                        {"trip_id": str(trip.id), "type": "ride_completed"},
                    )

                    # Receipt generation renders a PDF, uploads it to S3 and
                    # sends an email. That is a Celery job, not something to
                    # do while holding SELECT FOR UPDATE on this row.
                    from servers.ride.tasks import (
                        compute_trip_actuals, issue_receipt_for_trip,
                    )
                    trip_pk = trip.id
                    transaction.on_commit(
                        lambda: issue_receipt_for_trip.delay(trip_pk)
                    )

                    # Derive actual distance/duration out-of-band. OBSERVE
                    # ONLY: that task writes actual_* and never final_fare, so
                    # nothing here changes what the rider is charged.
                    #
                    # Delayed deliberately. The trail is drained by a periodic
                    # task, so the final stretch of points is not persisted yet
                    # at completion; computing immediately would under-read
                    # distance. The countdown gives the drain time to catch up.
                    # Also on_commit, so a slow or failing computation can
                    # never affect trip completion.
                    from django.conf import settings as _settings
                    actuals_delay = getattr(
                        _settings, 'TRIP_ACTUALS_DELAY_SECONDS', 180,
                    )
                    transaction.on_commit(
                        lambda: compute_trip_actuals.apply_async(
                            (trip_pk,), countdown=actuals_delay,
                        )
                    )
                elif status_code == 'cancelled':
                    trip.cancelled_at = timezone.now()
                    trip.cancelled_by = cancelled_by or 'system'
                    trip.cancellation_reason = cancellation_reason or ''
                    self._process_refund_on_cancel(trip)

                    Notification.objects.create(
                        user_id=trip.user_id,
                        title='Ride Cancelled',
                        message='Your ride has been cancelled.',
                    )
                    push = (
                        "Ride Cancelled",
                        "Your ride has been cancelled.",
                        {"trip_id": str(trip.id), "type": "ride_cancelled"},
                    )

                trip.save()

                # --- after-commit side effects -------------------------------
                # None of this may run if the transaction rolls back, and none
                # of it should run while the row lock is held.
                if push is not None:
                    rider = trip.user_id
                    title, body, data = push
                    transaction.on_commit(
                        lambda: send_push_notification(rider, title, body, data)
                    )

                trip_pk = trip.id
                driver_pk = trip.driver_id.id if trip.driver_id else None
                if status_code in ('completed', 'cancelled'):
                    def _clear_redis():
                        from servers.redis_client import (
                            clear_driver_active_trip, invalidate_trip,
                        )
                        from servers.ride.dispatch import dismiss_outstanding_offers
                        if driver_pk:
                            clear_driver_active_trip(driver_pk)
                        # Dismiss any candidate card still on screen as well
                        # as dropping the key; clear_offered_drivers() alone
                        # left candidates holding a dead offer.
                        dismiss_outstanding_offers(trip_pk, reason=status_code)
                        invalidate_trip(trip_pk)
                    transaction.on_commit(_clear_redis)
                else:
                    def _refresh_cache():
                        from servers.redis_client import cache_trip as _cache_trip
                        _cache_trip(trip_pk, status=status_code)
                    transaction.on_commit(_refresh_cache)

                result = {
                    'success': True,
                    'message': f'Trip {status_code}',
                    'driver_id': trip.driver_id_id if trip.driver_id else None,
                    'rider_id': trip.user_id_id,
                }

                # Include payment info for completed trips
                if status_code == 'completed':
                    payment = trip.payments.first()
                    if payment:
                        payment_info = {
                            'payment_id': payment.id,
                            'amount': str(payment.amount),
                            'method': payment.method,
                            'status': payment.status,
                            'gateway_order_id': payment.gateway_order_id,
                        }
                        
                        # Add payment options for deferred payments
                        if payment.method == 'deferred':
                            payment_info['payment_options'] = ['cash', 'wallet', 'online']
                            payment_info['requires_selection'] = True
                        
                        result['payment'] = payment_info

                return result
        except Trip.DoesNotExist:
            return {'success': False, 'error': 'Trip not found'}
        except Exception as e:
            logger.error(f"Error updating trip status: {str(e)}")
            return {'success': False, 'error': str(e)}

    def _create_payment_on_complete(self, trip):
        """Create a Payment record when trip is completed."""
        from django.conf import settings
        from servers.payments.models import Payment, TransactionHistory

        if trip.payments.exists():
            return

        amount = trip.final_fare or trip.estimated_fare
        if not amount:
            logger.warning(f"No fare amount for trip {trip.id}, skipping payment creation")
            return

        payment_method = trip.payment_method or 'deferred'

        if payment_method == 'deferred':
            # Create placeholder payment for deferred selection
            Payment.objects.create(
                trip_id=trip,
                user_id=trip.user_id,
                amount=amount,
                method='deferred',
                status='pending',
            )
            trip.payment_status = 'pending'
            trip.save(update_fields=['payment_status'])
            # No TransactionHistory or driver earnings yet - will be created when payment method is selected
        elif payment_method == 'cash':
            Payment.objects.create(
                trip_id=trip,
                user_id=trip.user_id,
                amount=amount,
                method='cash',
                status='pending',
            )
            trip.payment_status = 'pending'
            trip.save(update_fields=['payment_status'])

            # No TransactionHistory row here: credit_driver_wallet writes
            # exactly one, keyed on an idempotency key. Writing a second one
            # inline meant every cash trip was counted twice in GMV and in
            # the commission report — and a third time if the driver later
            # tapped "confirm cash".
            from servers.driver.utils import credit_driver_wallet
            credit_driver_wallet(trip)
        elif payment_method == 'wallet':
            from base.utils import wallet_payment
            
            wallet_result = wallet_payment(
                user=trip.user_id,
                amount=amount,
                purpose='Trip payment',
                reference_id=f'TRIP_{trip.id}',
                idempotency_key=f'trip_{trip.id}_payment'
            )
            
            if wallet_result.get('success'):
                Payment.objects.create(
                    trip_id=trip,
                    user_id=trip.user_id,
                    amount=amount,
                    method='wallet',
                    status='completed',
                )
                trip.payment_status = 'completed'
                trip.save(update_fields=['payment_status'])
                
                if trip.driver_id:
                    TransactionHistory.objects.create(
                        trip_id=trip,
                        user_id=trip.user_id,
                        driver_id=trip.driver_id,
                        amount=amount,
                        method='wallet',
                        user_name=trip.user_id.full_name or trip.user_id.phone_number,
                        status='completed',
                    )
                
                from servers.driver.utils import credit_driver_wallet
                credit_driver_wallet(trip)
            else:
                logger.error(f"Wallet payment failed for trip {trip.id}: {wallet_result.get('error')}")
                trip.payment_status = 'failed'
                trip.save(update_fields=['payment_status'])
        else:
            # Online payment: record the intent only. The Cashfree order is
            # created by POST /payments/create-order/, which the app calls
            # from the payment screen. Doing the gateway round-trip here put
            # a third-party HTTP call inside the trip-completion transaction
            # — if it timed out, the trip failed to complete; if the
            # transaction later rolled back, we had orphaned orders at
            # Cashfree with no local row.
            Payment.objects.create(
                trip_id=trip,
                user_id=trip.user_id,
                amount=amount,
                method='online',
                status='pending',
                payment_gateway=getattr(settings, 'PAYMENT_GATEWAY', 'cashfree'),
            )
            trip.payment_status = 'pending'
            trip.save(update_fields=['payment_status'])

    def _process_refund_on_cancel(self, trip):
        """Process refund if payment was completed online (delegates to shared utility)."""
        from servers.ride.utils import process_refund_on_cancel
        return process_refund_on_cancel(trip)


class AdminDashboardConsumer(LocationBroadcastMixin, AsyncWebsocketConsumer):
    """
    WebSocket consumer for Admin Dashboard real-time driver locations.
    
    Connect: ws://host/ws/admin/live-locations/?token=<jwt>
    """
    
    async def connect(self):
        self.user = self.scope.get('user')

        if isinstance(self.user, AnonymousUser) or not self.user.is_authenticated:
            logger.info("WS reject 4001: unauthenticated socket")
            await self.close(code=4001)
            return

        if not (self.user.is_staff or self.user.is_superuser):
            logger.info("WS reject 4003: user is not an admin")
            await self.close(code=4003)
            return

        # Accept the connection
        await self.accept()
        
        # Add to the admin_dashboard group
        self.admin_group = 'admin_dashboard'
        await self.channel_layer.group_add(self.admin_group, self.channel_name)

        # Send initial snapshot of all drivers
        from servers.redis_client import get_all_active_drivers
        drivers = await database_sync_to_async(get_all_active_drivers)()
        
        await self.send(text_data=json.dumps({
            'type': 'initial_drivers',
            'drivers': drivers
        }))
        await self._start_location_pump()

    async def disconnect(self, close_code):
        await self._stop_location_pump()
        if hasattr(self, 'admin_group'):
            await self.channel_layer.group_discard(self.admin_group, self.channel_name)

    async def receive(self, text_data):
        # Admin doesn't send data in this MVP
        pass

    async def driver_location_update(self, event):
        """
        Forward driver location updates to the admin client.

        A fleet monitor watching every driver is the heaviest consumer of
        location frames on the platform; it must not be able to apply
        backpressure to the loop that carries its other events too.
        """
        self._queue_location_frame(event)

    async def driver_status_update(self, event):
        """
        Forward driver status updates (e.g. offline) to the admin client.
        """
        await self.send(text_data=json.dumps(event))


