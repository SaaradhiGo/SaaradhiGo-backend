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


class DriverLocationConsumer(AsyncWebsocketConsumer):
    """
    WebSocket consumer for real-time driver location updates.
    
    Connect: ws://host/ws/driver/location/?token=<jwt>
    Send:    {"lng": 78.4867, "lat": 17.3850}
    """

    async def connect(self):
        self.user = self.scope.get('user')

        if isinstance(self.user, AnonymousUser) or not self.user.is_authenticated:
            print(f"WSREJECT 4001: User is anonymous or not auth. User: {self.user}")
            await self.close(code=4001)
            return

        # Verify user is a driver
        self.driver = await self._get_driver()
        if not self.driver:
            print("WSREJECT 4003: User is not a driver")
            await self.close(code=4003)
            return
        if self.driver.approved==False:
            # print("WSREJECT 4004: User is not approved")
            await self.close(code=4004)
            return
        lat = self.scope.get('lat')
        lng = self.scope.get('lng')
        if not(lat and lng):
            await self.close(code=4003)
        self.driver_id = self.driver.id
        self.driver_group = f'driver_{self.driver_id}'
        
        # Accept the connection early to prevent client timeout
        await self.accept()
        await self.send(text_data=json.dumps({
            'type': 'connection_established',
            'message': f'Driver {self.driver_id} connected',
        }))

        # Perform slower database/redis operations in the background of the connection
        await self._active_the_driver()
        
        await self._add_driver_location(lng, lat)
        
            
        # Join driver's personal group (for receiving ride requests)
        await self.channel_layer.group_add(self.driver_group, self.channel_name)
        # Join global online drivers group
        await self.channel_layer.group_add('online_drivers', self.channel_name)

    async def disconnect(self, close_code):
        if hasattr(self, 'driver_id'):
            await self._deactive_the_driver()
            # Remove from groups
            await self.channel_layer.group_discard(self.driver_group, self.channel_name)
            await self.channel_layer.group_discard('online_drivers', self.channel_name)
            # Remove from Redis geo index
            await self._remove_driver_location()
            logger.info(f"Driver {self.driver_id} disconnected")

    async def receive(self, text_data):
        """
        Receive location update from driver.
        Expected: {"lng": float, "lat": float}
        """
        try:
            data = json.loads(text_data)
            print(data)
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
                await self.send(text_data=json.dumps({
                    'type': 'location_updated',
                    'lng': lng,
                    'lat': lat,
                }))
                
                # If driver is on an active trip, stream location to the rider
                active_trip_id = result.get('active_trip_id')
                if active_trip_id:
                    await self.channel_layer.group_send(f'trip_{active_trip_id}', {
                        'type': 'driver_location_update',
                        'lng': lng,
                        'lat': lat,
                        'driver_id': self.driver_id,
                    })
            else:
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
        }))

    # -- Database helpers --

    @database_sync_to_async
    def _get_driver(self):
        try:
            return self.user.driver
        except Exception as e:
            print(f"Error in _get_driver: {e}")
            return None
    @database_sync_to_async
    def _active_the_driver(self):
        try:
            self.driver.status = "online"
            self.driver.save()
        except Exception as e:
            print(f"Error in _active_the_driver: {e}")
            return None
    @database_sync_to_async
    def _deactive_the_driver(self):
        try:
            self.driver.status = "off"
            self.driver.save()
        except Exception as e:
            print(f"Error in _deactive_the_driver: {e}")
            return None
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

class RideRequestConsumer(AsyncWebsocketConsumer):
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

    async def disconnect(self, close_code):
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

            # Validate required fields
            if not all([pickup_lat, pickup_lng, destination_lat, destination_lng, pickup_address, destination_address]):
                await self.send(text_data=json.dumps({
                    'type': 'error',
                    'message': 'pickup_lat, pickup_lng, destination_lat, destination_lng, pickup_address, destination_address are required'
                }))
                return

            # Create trip in database
            trip = await self._create_trip(
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
            )

            if not trip:
                await self.send(text_data=json.dumps({
                    'type': 'error',
                    'message': 'Failed to create trip'
                }))
                return

            # Confirm trip creation to rider
            await self.send(text_data=json.dumps({
                'type': 'trip_created',
                'trip_id': trip.id,
                'estimated_fare': str(trip.estimated_fare) if trip.estimated_fare else None,
                'message': 'Searching for nearby drivers...'
            }))

            # Log ride request to Redis Stream (for future analytics, not driver notification)
            await self._publish_ride_request(trip)

            # Find and notify nearby drivers via WebSocket
            notified_count = await self._notify_nearby_drivers(
                trip=trip,
                pickup_lng=float(pickup_lng),
                pickup_lat=float(pickup_lat),
                destination_lat=destination_lat,
                destination_lng=destination_lng,
                pickup_address=pickup_address,
                destination_address=destination_address,
                vehicle_type=vehicle_type,
            )

            if notified_count > 0:
                await self.send(text_data=json.dumps({
                    'type': 'drivers_notified',
                    'trip_id': trip.id,
                    'drivers_notified': notified_count,
                    'message': f'{notified_count} nearby driver(s) notified'
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
        """
        Retry notifying nearby drivers for an existing pending trip.
        Expected: {"action": "retry", "trip_id": int, "radius": int (optional, meters)}
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

        await self.send(text_data=json.dumps({
            'type': 'retry_started',
            'trip_id': trip.id,
            'radius': radius,
            'message': 'Retrying — searching for nearby drivers...'
        }))

        # Get vehicle type from the trip's requested_vehicle_type
        vt_name = None
        if trip.requested_vehicle_type:
            vt_name = await database_sync_to_async(lambda: trip.requested_vehicle_type.type)()

        notified_count = await self._notify_nearby_drivers(
            trip=trip,
            pickup_lng=float(trip.pickup_long),
            pickup_lat=float(trip.pickup_lat),
            destination_lat=float(trip.destination_lat),
            destination_lng=float(trip.destination_long),
            pickup_address=trip.pickup_address or '',
            destination_address=trip.destination_address or '',
            radius=radius,
            vehicle_type=vt_name,
        )

        if notified_count > 0:
            await self.send(text_data=json.dumps({
                'type': 'drivers_notified',
                'trip_id': trip.id,
                'drivers_notified': notified_count,
                'message': f'Retry: {notified_count} nearby driver(s) notified'
            }))

    async def _notify_nearby_drivers(self, trip, pickup_lng, pickup_lat,
                                      destination_lat, destination_lng,
                                      pickup_address, destination_address,
                                      radius=5000, vehicle_type=None):
        """
        Find nearby drivers and send them a ride request via WebSocket.
        Filters by vehicle_type if provided.
        Returns the number of drivers notified.
        """
        # Fetch more candidates from Redis to allow for post-filtering
        nearby = await self._find_nearby_drivers(pickup_lng, pickup_lat, vehicle_type)

        if not nearby:
            await self.send(text_data=json.dumps({
                'type': 'no_drivers',
                'trip_id': trip.id,
                'message': 'No nearby drivers found. Please try again shortly.'
            }))
            return 0

        # Extract driver IDs from Redis results
        all_driver_ids = []
        for driver_info in nearby:
            driver_key = driver_info[0] if isinstance(driver_info, (list, tuple)) else driver_info
            if isinstance(driver_key, str) and driver_key.startswith('driver:'):
                all_driver_ids.append(driver_key.split(':')[1])

        rider_name = await self._get_rider_name()
        notified_count = 0
        for driver_id in all_driver_ids:
            driver_group = f'driver_{driver_id}'
            await self.channel_layer.group_send(driver_group, {
                'type': 'ride_request',
                'trip_id': trip.id,
                'rider_name': rider_name,
                'pickup_lat': str(pickup_lat),
                'pickup_lng': str(pickup_lng),
                'destination_lat': str(destination_lat),
                'destination_lng': str(destination_lng),
                'pickup_address': pickup_address,
                'destination_address': destination_address,
                'estimated_fare': str(trip.estimated_fare) if trip.estimated_fare else '',
            })
            notified_count += 1

            await self._send_driver_push(
                driver_id,
                "New Ride Request",
                f"New ride request from {rider_name}",
                {"trip_id": str(trip.id), "type": "ride_request"}
            )

        return notified_count

    # -- Event handlers --

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
        """Send live driver location to rider."""
        await self.send(text_data=json.dumps({
            'type': 'driver_location_update',
            'lng': event['lng'],
            'lat': event['lat'],
            'driver_id': event['driver_id'],
        }))
        
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
                     distance_km=None, duration_min=None, vehicle_type=None, payment_method='deferred'):
        from decimal import Decimal
        from servers.ride.models import Trip, FarePricing
        from servers.ride.utils import estimate_amount, validate_distance
        from servers.driver.models import VehicleType
        from django.db import transaction

        try:
            # Parse distance and duration
            try:
                dist = float(distance_km) if distance_km is not None else 0
                dur = float(duration_min) if duration_min is not None else 0
            except (ValueError, TypeError):
                dist, dur = 0, 0

            is_valid, validated_km, validated_min, msg = validate_distance(dist, dur, pickup_lat, pickup_lng, destination_lat, destination_lng)
            if not is_valid:
                logger.warning(f"Distance spoofing attempt: {msg}")
                raise ValueError(f"Invalid distance: {msg}")

            fare = estimate_amount(
                dist, 
                dur, 
                vehicle_type=vehicle_type,
                pickup_lat=pickup_lat,
                pickup_long=pickup_lng,
                rider_id=self.user.id
            )

            # Resolve VehicleType for storing on Trip
            requested_vt = None
            if vehicle_type:
                requested_vt = VehicleType.objects.filter(type__iexact=vehicle_type).first()

            with transaction.atomic():
                trip = Trip.objects.create(
                    user_id=self.user,
                    pickup_lat=pickup_lat,
                    pickup_long=pickup_lng,
                    destination_lat=destination_lat,
                    destination_long=destination_lng,
                    pickup_address=pickup_address,
                    destination_address=destination_address,
                    estimated_fare=fare['total_fare'],
                    estimated_distance_km=Decimal(str(dist)) if dist else None,
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

            # Schedule auto-cancel task
            from servers.ride.tasks import auto_cancel_trip
            from django.conf import settings
            auto_cancel_trip.apply_async((trip.id,), countdown=settings.TRIP_ACCEPT_TIMEOUT_SECONDS)

            return trip
        except Exception as e:
            logger.error(f"Failed to create trip: {str(e)}")
            return None

    @database_sync_to_async
    def _find_nearby_drivers(self, lng, lat, vehicle_type=None):
        from servers.redis_client import nearby_drivers
        return nearby_drivers(lng=lng, lat=lat, vehicle_type=vehicle_type)

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


class TripStatusConsumer(AsyncWebsocketConsumer):
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

        # Verify user is part of this trip
        is_participant = await self._is_trip_participant()
        if not is_participant:
            await self.close(code=4003)
            return

        await self.channel_layer.group_add(self.trip_group, self.channel_name)

        await self.accept()
        await self.send(text_data=json.dumps({
            'type': 'connection_established',
            'trip_id': self.trip_id,
            'message': 'Connected to trip updates',
        }))

    async def disconnect(self, close_code):
        if hasattr(self, 'trip_group'):
            await self.channel_layer.group_discard(self.trip_group, self.channel_name)

    async def receive(self, text_data):
        """
        Receive trip actions from driver.
        Expected: {"action": "accept|reached|start|complete|cancel"}
        """
        try:
            data = json.loads(text_data)
            action = data.get('action')
            otp_input = data.get('otp')

            if action not in ('accept', 'reached', 'start', 'complete', 'cancel', 'confirm_cash'):
                await self.send(text_data=json.dumps({
                    'type': 'error',
                    'message': 'Invalid action. Must be: accept, reached, start, complete, cancel, or confirm_cash'
                }))
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
                if not await self._is_driver():
                    await self.send(text_data=json.dumps({
                        'type': 'error',
                        'message': 'Only drivers can accept rides'
                    }))
                    return
                result = await self._accept_trip()
            elif action == 'reached':
                if not await self._is_assigned_driver():
                    await self.send(text_data=json.dumps({
                        'type': 'error',
                        'message': 'Only the assigned driver can mark this trip as reached'
                    }))
                    return
                result = await self._update_trip_status('reached')
            elif action == 'start':
                if not await self._is_assigned_driver():
                    await self.send(text_data=json.dumps({
                        'type': 'error',
                        'message': 'Only the assigned driver can start this trip'
                    }))
                    return
                if not otp_input:
                    await self.send(text_data=json.dumps({
                        'type': 'error',
                        'message': 'OTP is required to start the ride'
                    }))
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
                    return
                result = await self._update_trip_status('completed')
            elif action == 'confirm_cash':
                if not await self._is_assigned_driver():
                    await self.send(text_data=json.dumps({
                        'type': 'error',
                        'message': 'Only the assigned driver can confirm cash payment'
                    }))
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
                    return
                result = await self._update_trip_status('cancelled')

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
            else:
                await self.send(text_data=json.dumps({
                    'type': 'error',
                    'message': result.get('error', 'Action failed')
                }))

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
        """Broadcast live driver location to riders on this trip."""
        await self.send(text_data=json.dumps({
            'type': 'driver_location_update',
            'lng': event['lng'],
            'lat': event['lat'],
            'driver_id': event['driver_id'],
        }))
        
    async def cash_payment_confirmed(self, event):
        """Notify rider that cash payment has been confirmed."""
        await self.send(text_data=json.dumps({
            'type': 'cash_payment_confirmed',
            'trip_id': event['trip_id'],
            'message': 'Cash payment has been confirmed by driver',
        }))

    # -- Database helpers --

    @database_sync_to_async
    def _is_trip_participant(self):
        from servers.ride.models import Trip
        try:
            trip = Trip.objects.get(id=self.trip_id)
            # Rider check
            if trip.user_id_id == self.user.id:
                return True
            # Driver check
            if trip.driver_id and hasattr(self.user, 'driver'):
                return trip.driver_id_id == self.user.driver.id
            # Allow any driver to join if no driver assigned yet (for accepting)
            if not trip.driver_id and hasattr(self.user, 'driver'):
                return True
            return False
        except Trip.DoesNotExist:
            return False

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
        from django.utils import timezone
        from django.db import transaction

        try:
            with transaction.atomic():
                trip = Trip.objects.select_for_update().get(id=self.trip_id)

                # Check if already accepted
                if trip.driver_id is not None:
                    return {'success': False, 'error': 'Trip already accepted by another driver'}

                driver = self.user.driver
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
                trip = Trip.objects.select_for_update().get(id=self.trip_id)
                payment = trip.payments.filter(method='cash').first()
                if not payment:
                    return {'success': False, 'error': 'No cash payment found for this trip'}
                
                payment.status = 'completed'
                payment.save()
                
                # Create transaction history
                TransactionHistory.objects.create(
                    trip_id=trip,
                    user_id=trip.user_id,
                    driver_id=trip.driver_id,
                    amount=payment.amount,
                    method='cash',
                    user_name=trip.user_id.full_name or trip.user_id.phone_number,
                    status='completed',
                    txn_type='credit'
                )
                
                # Credit driver's wallet
                from servers.driver.utils import credit_driver_wallet
                credit_driver_wallet(trip)
                
            return {'success': True, 'message': 'Cash payment confirmed', 'rider_id': trip.user_id_id}
        except Trip.DoesNotExist:
            return {'success': False, 'error': 'Trip not found'}
        except Exception as e:
            logger.error(f"Error confirming cash payment: {str(e)}", exc_info=True)
            return {'success': False, 'error': str(e)}
            
    @database_sync_to_async
    def _update_trip_status(self, status_code, otp_input=None):
        from servers.ride.models import Trip, TripStatus
        from django.utils import timezone
        from django.db import transaction

        try:
            with transaction.atomic():
                # select_for_update() locks the row until the transaction ends
                trip = Trip.objects.select_for_update().get(id=self.trip_id)
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
                            'error': f'Invalid status transition: cannot change from {current_status} to {status_code}'
                        }

                if status_code == 'cancelled' and current_status in ['completed', 'cancelled']:
                    return {'success': False, 'error': f'Trip is already {current_status}'}

                if status_code == 'in_progress':
                    if str(trip.otp) != str(otp_input):
                        return {
                            'success': False,
                            'error': 'Invalid OTP provided'
                        }
                        
                status_obj, _ = TripStatus.objects.get_or_create(
                    status_code=status_code,
                    defaults={'description': f'Trip {status_code}'}
                )

                trip.status_id = status_obj

                # Set timestamps based on status
                if status_code == 'reached':
                    trip.reached_at = timezone.now()
                    from servers.auth_user.services import send_push_notification
                    send_push_notification(
                        trip.user_id,
                        "Driver Arrived",
                        "Your driver has arrived at the pickup location.",
                        {"trip_id": str(trip.id), "type": "driver_arrived"}
                    )
                elif status_code == 'in_progress':
                    trip.started_at = timezone.now()
                    from servers.auth_user.services import send_push_notification
                    send_push_notification(
                        trip.user_id,
                        "Ride Started",
                        "Your ride is now in progress.",
                        {"trip_id": str(trip.id), "type": "ride_started"}
                    )
                elif status_code == 'completed':
                    trip.completed_at = timezone.now()
                    # Create payment on trip completion within the same transaction
                    self._create_payment_on_complete(trip)
                    
                    from servers.rider.models import Notification
                    Notification.objects.create(
                        user_id=trip.user_id,
                        title='Ride Completed',
                        message=f'Your ride has been completed. Final fare: ₹{trip.final_fare or trip.estimated_fare}',
                    )
                    
                    from servers.auth_user.services import send_push_notification
                    send_push_notification(
                        trip.user_id,
                        "Ride Completed",
                        f"Your ride has been completed. Final fare: ₹{trip.final_fare or trip.estimated_fare}",
                        {"trip_id": str(trip.id), "type": "ride_completed"}
                    )
                elif status_code == 'cancelled':
                    trip.cancelled_at = timezone.now()
                    self._process_refund_on_cancel(trip)
                    
                    from servers.rider.models import Notification
                    Notification.objects.create(
                        user_id=trip.user_id,
                        title='Ride Cancelled',
                        message=f'Your ride has been cancelled.',
                    )
                    
                    from servers.auth_user.services import send_push_notification
                    send_push_notification(
                        trip.user_id,
                        "Ride Cancelled",
                        "Your ride has been cancelled.",
                        {"trip_id": str(trip.id), "type": "ride_cancelled"}
                    )

                trip.save()

                # If trip has ended, restore driver's available status + invalidate cache
                if status_code in ('completed', 'cancelled') and trip.driver_id:
                    from servers.redis_client import clear_driver_active_trip, invalidate_trip
                    clear_driver_active_trip(trip.driver_id.id)
                    invalidate_trip(trip.id)
                else:
                    # For intermediate states, update cache with new status
                    from servers.redis_client import cache_trip as _cache_trip
                    _cache_trip(trip.id, status=status_code)

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

            if trip.driver_id:
                TransactionHistory.objects.create(
                    trip_id=trip,
                    user_id=trip.user_id,
                    driver_id=trip.driver_id,
                    amount=amount,
                    method='cash',
                    user_name=trip.user_id.full_name or trip.user_id.phone_number,
                    status='completed',
                )
            
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
            from servers.payments.payment_gateways.factory import get_payment_gateway

            gateway = get_payment_gateway()
            order_result = gateway.create_order(
                amount=amount,
                currency='INR',
                receipt=f'trip_{trip.id}',
                notes={'trip_id': str(trip.id), 'user_id': str(trip.user_id.id)}
            )
            Payment.objects.create(
                trip_id=trip,
                user_id=trip.user_id,
                amount=amount,
                method='online',
                status='pending',
                gateway_order_id=order_result.get('order_id') if order_result else None,
                payment_gateway='cashfree'
            )
            trip.payment_status = 'pending'
            trip.save(update_fields=['payment_status'])

    def _process_refund_on_cancel(self, trip):
        """Process refund if payment was completed online."""
        from servers.payments.models import Payment
        from servers.payments.payment_gateways.factory import get_payment_gateway
        from servers.rider.models import Notification

        payment = Payment.objects.filter(trip_id=trip, method='online', status='completed').first()
        if payment and payment.gateway_payment_id:
            gateway = get_payment_gateway()
            refund = gateway.create_refund(payment.gateway_payment_id)
            if refund:
                payment.status = 'refunded'
                payment.save()
                
                trip.payment_status = 'refunded'
                trip.save(update_fields=['payment_status'])
                
                Notification.objects.create(
                    user_id=trip.user_id,
                    title='Refund Processed',
                    message=f'Your refund of ₹{payment.amount} has been initiated due to cancellation.',
                )

