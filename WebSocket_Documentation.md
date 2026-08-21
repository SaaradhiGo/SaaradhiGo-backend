# VahanGo WebSocket Integration Guide

This document outlines the real-time (WebSockets) Interaction Flow for the VahanGo platform.
It details the sequence of events, connections, request payloads, and server-emitted broadcast events.

---

## 1. Driver Location System (``)
This consumer tracks the driver's real-time live location, activates them to an "online" status upon connection, and receives incoming ride requests.

**Endpoint:** `ws://<host>/ws/driver/location/?token=<JWT>&lat=<lat>&lng=<lng>`
**Role:** Driver Only

### Connection Flow
1. Connect with JWT Token, latitude, and longitude.
2. Server verifies `Driver` status. Driver is added to spatial indexing and marked as `online`.
3. **Response on Success:**
   ```json
   {
       "type": "connection_established",
       "message": "Driver <ID> connected"
   }
   ```

### Client -> Server Events
**Action: Update Location**
Emitted periodically by the driver application while moving.
```json
{
    "lng": 78.4867,
    "lat": 17.3850
}
```

### Server -> Client Broadcasts
**Event: Location Updated Callback**
Confirmed after successfully storing in the backend Redis cache.
```json
{
    "type": "location_updated",
    "lng": 78.4867,
    "lat": 17.3850
}
```

**Event: Incoming Ride Request**
Pushed to the driver when a rider successfully sends a request nearby.
```json
{
    "type": "ride_request",
    "trip_id": 101,
    "rider_name": "Raja Kumar",
    "pickup_lat": 17.385,
    "pickup_lng": 78.486,
    "destination_lat": 17.440,
    "destination_lng": 78.348,
    "pickup_address": "Ameerpet Metro",
    "destination_address": "Hitech City",
    "estimated_fare": "245.50"
}
```

---

## 2. Rider Request Flow (`RideRequestConsumer`)
This consumer allows a rider to establish an active session, initiate a ride request, retry driver matching, and consume live driver location streams.

**Endpoint:** `ws://<host>/ws/ride/request/?token=<JWT>`
**Role:** Rider Only

### Connection Flow
1. Connect with JWT.
2. **Response on Success:**
   ```json
   {
       "type": "connection_established",
       "message": "Rider connected, ready for ride requests"
   }
   ```

### Client -> Server Events

**Action: Request New Ride**
Triggers pricing, trip creation, and notifies nearby drivers via the server.
```json
{
    "action": "request",
    "pickup_lat": 17.385,
    "pickup_lng": 78.486,
    "destination_lat": 17.440,
    "destination_lng": 78.348,
    "pickup_address": "Ameerpet Metro",
    "destination_address": "Hitech City",
    "distance_km": 10.5,
    "duration_min": 25.0,
    "vehicle_type": "sedan",
    "payment_method": "cash"
}
```

**Action: Retry Request**
Retry broadcasting intended targeting a timeout or wider ping.
```json
{
    "action": "retry",
    "trip_id": 101,
    "radius": 5000
}
```

### Server -> Client Broadcasts
**Event: Trip Created (Intermediate State)**
```json
{
    "type": "trip_created",
    "trip_id": 101,
    "estimated_fare": "245.50",
    "message": "Searching for nearby drivers..."
}
```

**Event: Drivers Notified (Success Matching)**
```json
{
    "type": "drivers_notified",
    "trip_id": 101,
    "drivers_notified": 3,
    "message": "3 nearby driver(s) notified"
}
```

**Event: No Drivers Found (Failure Matching)**
```json
{
    "type": "no_drivers",
    "trip_id": 101,
    "message": "No nearby drivers found. Please try again shortly."
}
```

**Event: Trip Update (Status Transitions)**
Triggered when the driver accepts the ride, marks reached, starts, or completes it. (Proxied via the TripStatusConsumer)
```json
{
    "type": "trip_update",
    "trip_id": 101,
    "status": "accept",
    "message": "Driver accepted the ride",
    "driver_id": 15,
    "driver_name": "Driver A",
    "otp": "123456",
    "driver_info": { "name": "Driver A", "phone": "+91..." },
    "vehicle_info": { "model": "Swift", "number": "TS07AB1234" }
}
```

**Event: Driver Live Tracking Update**
Once a trip is accepted, the specific driver's location updates stream out dynamically here.
```json
{
    "type": "driver_location_update",
    "lng": 78.4867,
    "lat": 17.3850,
    "driver_id": 15
}
```

---

## 3. Trip Status Consumer (`TripStatusConsumer`)
This consumer manages explicit trip state tracking between a driver and rider once a trip has an initial ID associated. 

**Endpoint:** `ws://<host>/ws/ride/trip/<trip_id>/?token=<JWT>`
**Role:** Rider & Driver

### Connection Flow
1. **Response on Success:**
   ```json
   {
       "type": "connection_established",
       "trip_id": 101,
       "message": "Connected to trip updates"
   }
   ```

### Client -> Server Events (Driver Explicit Operations)

The driver application actively drives state machine changes. Send to server:

**Action: Accept Ride**
```json
{ "action": "accept" }
```

**Action: Driver Reached Location**
```json
{ "action": "reached" }
```

**Action: Start Ride (OTP Verification)**
```json
{ 
    "action": "start",
    "otp": "123456" 
}
```

**Action: Complete Ride**
```json
{ "action": "complete" }
```

**Action: Cancel Ride**
```json
{ "action": "cancel" }
```

### Server -> Client Broadcasts (Rider & Driver Audience)

**Event: Status Updated Broadcast**
Emitted to all participants inside the `trip_id` socket group successfully evaluating an operation.

```json
{
    "type": "trip_status_update",
    "trip_id": 101,
    "status": "accept|reached|in_progress|completed|cancelled",
    "message": "Status contextual description message",
    "driver_id": 15
}
```
*(Note: Initial `accept` payload also includes `otp`, `driver_info` and `vehicle_info` similarly to the `trip_update` payload in the Request Consumer)*

---

## 4. Admin Fleet Monitor & Dashboard (`AdminDashboardConsumer`)
This consumer powers the God View / Fleet Monitor (`fleet_monitor.html`) and predictive dashboards, streaming live driver locations and system events across the fleet.

**Endpoint:** `ws://<host>/ws/admin/dashboard/` (supports Django session cookies or `?token=<JWT>`)
**Role:** Admin / Superuser Only

### Connection Flow
1. Connect via browser (session cookie) or with admin JWT token in query string.
2. Server verifies `admin` role or `superuser` status.
3. **Response on Success:**
   ```json
   {
       "type": "connection_established",
       "message": "Connected to live fleet telemetry stream"
   }
   ```
4. **Initial Drivers Snapshot (Immediately pushed on connection):**
   ```json
   {
       "type": "initial_drivers",
       "count": 2,
       "drivers": [
           {
               "driver_id": 15,
               "driver_name": "Ramesh Kumar",
               "phone_number": "+919876543210",
               "ratings": "4.85",
               "vehicle_model": "Swift Dzire",
               "vehicle_number": "TS09EA1234",
               "lat": 17.3850,
               "lng": 78.4867,
               "status": "online"
           }
       ]
   }
   ```

### Client -> Server Events
**Action: Refresh Driver Snapshot**
```json
{ "action": "refresh" }
```

**Action: Heartbeat Ping**
```json
{ "action": "ping" }
```

### Server -> Client Broadcasts
**Event: Driver Location Update (Live GPS telemetry)**
```json
{
    "type": "driver_location_update",
    "driver_id": 15,
    "driver_name": "Ramesh Kumar",
    "phone_number": "+919876543210",
    "vehicle_model": "Swift Dzire",
    "vehicle_number": "TS09EA1234",
    "lat": 17.3854,
    "lng": 78.4871,
    "status": "online",
    "active_trip_id": null
}
```

**Event: Driver Status Update (Offline / Online)**
```json
{
    "type": "driver_status_update",
    "driver_id": 15,
    "status": "offline"
}
```

