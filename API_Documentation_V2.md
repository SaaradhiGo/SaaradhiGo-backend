# VahanGo API Documentation V2

This document provides exhaustive, unrestrictive details for every API endpoint in the VahanGo platform.
Each endpoint includes required variables, optional variables, and fully expanded request and response samples.
**Base-URL**: https://dev.api.saaradhigo.in/
---
## Universal Guidelines
- **Content-Type**: `application/json` (except for profile/vehicle file uploads which require `multipart/form-data`).
- **Authorization**: Endpoints specifying `Auth Required: Yes` must include the header `Authorization: Bearer <access_token>`.
- **Response Format**: All successful responses return a `{"status": "success", "data": { ... }}` envelope. Error responses return standard `code`, `message`, `issue`, and `field` objects.

---

## 1. Authentication (App: `auth_user`)

### 1.1 Request OTP
Generate an OTP. Sends via SNS.
- **URL**: `/auth/otp/`
- **Method**: `POST`
- **Auth Required**: No

**Parameters**:
| Name | Type | Required | Description |
| ---- | ---- | -------- | ----------- |
| `phone_number` | string | **Yes** | E.164 formatted number (e.g., `+919876543210`). |
| `role` | string | No | `rider` or `driver`. Default: `rider`. |

**Sample Request**:
```json
{
  "phone_number": "+919876543210",
  "role": "rider"
}
```
**Sample Response (200 OK)**:
```json
{
  "status": "success",
  "data": {
    "message": "OTP sent successfully",
    "task_id": "848e029f-...",
    "expires_in": 600
  }
}
```

> **Note:** The OTP is delivered exclusively via SMS. It is never returned in this response and never logged server-side. Earlier versions of this endpoint echoed the OTP back for development convenience; that behaviour has been removed for security.

### 1.2 Login & Token Generation
Verify OTP, create user if missing, setup profile, return JWT.
- **URL**: `/auth/login/`
- **Method**: `POST`
- **Auth Required**: No

**Parameters**:
| Name | Type | Required | Description |
| ---- | ---- | -------- | ----------- |
| `phone_number`| string | **Yes** | Phone number used to request OTP. |
| `otp` | string | **Yes** | Received OTP. |
| `device_token`| string | No | FCM Device Token for notifications. |
| `password` | string | No | Account password. |

**Sample Request**:
```json
{
  "phone_number": "+919876543210",
  "otp": "123456",
  "device_token": "fcm_token_xyz"
}
```
**Sample Response (200 OK)**:
```json
{
  "status": "success",
  "data": {
    "token": "eyJhbGciOi...",
    "refresh_token": "eyJhbG..",
    "user": {
      "id": 1,
      "username": "user_1",
      "full_name": null,
      "phone_number": "+919876543210",
      "email": null,
      "gender": null,
      "dob": null,
      "house_no": null,
      "street": null,
      "city": null,
      "zip_code": null,
      "emergency_contact": null,
      "role": "rider",
      "avatar": null,
      "fcm_token": "fcm_token_xyz",
      "updated_at": "2026-04-07T10:00:00Z",
      "created_at": "2026-04-07T10:00:00Z",
      "is_updated": false
    }
  }
}
```

### 1.3 Refresh Token
Exchange refresh token for a new access token.
- **URL**: `/auth/refresh/`
- **Method**: `POST`
- **Auth Required**: No

**Sample Request**:
```json
{
  "refresh_token": "eyJhbGciOi..."
}
```
**Sample Response (200 OK)**:
```json
{
  "status": "success",
  "data": {
    "token": "eyJ_new_access",
    "refresh_token": "eyJ_new_refresh"
  }
}
```

### 1.4 Update User
Update user demographic data. Supports `multipart/form-data` for files.
- **URL**: `/auth/update/`
- **Method**: `PATCH`
- **Auth Required**: Yes

**Parameters (All Optional)**: `full_name`, `email`, `gender`, `dob`, `house_no`, `street`, `city`, `zip_code`, `emergency_contact`, `phone_number`, `avatar` (file).

**Sample Request (JSON)**:
```json
{
  "full_name": "Raja Kumar",
  "email": "raja@example.com",
  "city": "Hyderabad"
}
```
**Sample Response (200 OK)**:
```json
{
  "status": "success",
  "data": {
    "id": 1,
    "full_name": "Raja Kumar",
    "email": "raja@example.com",
    "city": "Hyderabad"
    ...
  }
}
```

### 1.5 Get User Profile
Fetch the current authenticated user's profile.
- **URL**: `/auth/profile/`
- **Method**: `GET`
- **Auth Required**: Yes

**Sample Request**: `GET /auth/profile/` (with Auth bearer header).
**Sample Response (200 OK)**: Same output block as User Object in `1.2 Login`.

### 1.6 Admin: List Users
Fetch all users. Requires Admin permissions.
- **URL**: `/auth/admin/users/`
- **Method**: `GET`
- **Auth Required**: Yes (IsAdmin)

**Query Parameters**:
| Name | Type | Required | Description |
| ---- | ---- | -------- | ----------- |
| `role` | string | No | Filter by role (`rider`, `driver`, `admin`). |
| `is_active` | bool | No | Filter active status (`true`/`false`). |
| `page_size`, `page` | int | No | Pagination control. |

**Sample Request**: `GET /auth/admin/users/?role=rider&page_size=10`
**Sample Response (200 OK)**:
```json
{
  "status": "success",
  "data": {
    "count": 50,
    "next": "http://api/auth/admin/users/?page=2",
    "previous": null,
    "results": [
      {
        "id": 1,
        "full_name": "Test User",
        "role": "rider",
        ...
      }
    ]
  }
}
```

---

## 2. Rider Functions (App: `rider`)

### 2.1 Save Favorite Location
- **URL**: `/rider/locations/`
- **Method**: `POST`
- **Auth Required**: Yes

**Parameters**:
| Name | Type | Required | Description |
| ---- | ---- | -------- | ----------- |
| `address_text` | string | **Yes** | Human readable address. |
| `latitude` | float | **Yes** | Latitude |
| `longitude` | float | **Yes** | Longitude |

**Sample Request**:
```json
{
  "address_text": "Ameerpet Metro",
  "latitude": 17.4359,
  "longitude": 78.4449
}
```
**Sample Response (201 Created)**:
```json
{
  "status": "success",
  "data": {
    "location": {
      "id": 5,
      "user_id": 1,
      "address_text": "Ameerpet Metro",
      "latitude": "17.4359",
      "longitude": "78.4449"
    }
  }
}
```

### 2.2 Get All Favorite Locations
- **URL**: `/rider/locations/all/`
- **Method**: `GET`
- **Auth Required**: Yes

**Sample Request**: `GET /rider/locations/all/`
**Sample Response (200 OK)**:
```json
{
  "status": "success",
  "data": [
    {
      "id": 5,
      "address_text": "Ameerpet Metro",
      "latitude": "17.4359",
      "longitude": "78.4449"
    }
  ]
}
```

### 2.3 Delete Favorite Location
- **URL**: `/rider/locations/<location_id>/delete/`
- **Method**: `DELETE`
- **Auth Required**: Yes

**Sample Request**: `DELETE /rider/locations/5/delete/`
**Sample Response (200 OK)**:
```json
{
  "status": "success",
  "data": {
    "message": "Favorite location deleted successfully"
  }
}
```

### 2.4 Get Nearby Drivers
Uses Redis to fetch active online drivers within the radius.
- **URL**: `/rider/nearby/`
- **Method**: `GET`
- **Auth Required**: Yes

**Query Parameters**:
| Name | Type | Required | Description |
| ---- | ---- | -------- | ----------- |
| `lng`, `lat` | float | **Yes** | User location coordinates. |
| `radius` | int | No | Default 1000m. |
| `count` | int | No | Default 10 max drivers. |

**Sample Request**: `GET /rider/nearby/?lat=17.4&lng=78.4&radius=2000`
**Sample Response (200 OK)**:
```json
{
  "status": "success",
  "data": [
    ["driver:15", "110.50"],
    ["driver:18", "450.25"]
  ]
}
```

### 2.5 List Notifications
- **URL**: `/rider/notifications/`
- **Method**: `GET`
- **Auth Required**: Yes
- Supports Pagination (`page`, `page_size`).

**Sample Request**: `GET /rider/notifications/`
**Sample Response (200 OK)**:
```json
{
  "status": "success",
  "data": {
    "count": 2,
    "next": null,
    "previous": null,
    "results": [
      {
        "id": 1,
        "title": "Welcome",
        "message": "Welcome to VahanGo!",
        "is_read": false,
        "created_at": "2026-04-07T10:00:00Z"
      }
    ]
  }
}
```

### 2.6 Mark Notification Read
- **URL**: `/rider/notifications/<notif_id>/read/`
- **Method**: `PATCH`
- **Auth Required**: Yes

**Sample Request**: `PATCH /rider/notifications/1/read/`
**Sample Response (200 OK)**:
```json
{
  "status": "success",
  "data": { "message": "Marked as read" }
}
```

### 2.7 Mark All Notifications Read
- **URL**: `/rider/notifications/read-all/`
- **Method**: `POST`
- **Auth Required**: Yes

**Sample Request**: `POST /rider/notifications/read-all/`
**Sample Response (200 OK)**:
```json
{
  "status": "success",
  "data": { "message": "All notifications marked as read" }
}
```

### 2.8 Get Wallet Balance
- **URL**: `/rider/wallet/balance/`
- **Method**: `GET`
- **Auth Required**: Yes

**Sample Request**: `GET /rider/wallet/balance/`
**Sample Response (200 OK)**:
```json
{
  "status": "success",
  "data": {
    "balance": "350.00"
  }
}
```

### 2.9 Create Wallet Order
Initiates a Cashfree order for adding money to the wallet.
- **URL**: `/rider/wallet/create-order/`
- **Method**: `POST`
- **Auth Required**: Yes

**Parameters**:
| Name | Type | Required | Description |
| ---- | ---- | -------- | ----------- |
| `amount` | string | **Yes** | Amount to add. |

**Sample Request**:
```json
{ "amount": "500.00" }
```
**Sample Response (201 Created)**:
```json
{
  "status": "success",
  "data": {
    "transaction_id": 1,
    "gateway_order_id": "order_Fxy...",
    "amount": "500.00",
    "currency": "INR",
    "description": "Wallet Top-up",
    "payment_session_id": "session_xyz...",
    "order_token": "token_abc...",
    "gateway": "cashfree"
  }
}
```

### 2.10 Verify Wallet Payment
Secures the transaction and augments the wallet balance upon success.
- **URL**: `/rider/wallet/verify/`
- **Method**: `POST`
- **Auth Required**: Yes

**Parameters**:
| Name | Type | Required | Description |
| ---- | ---- | -------- | ----------- |
| `gateway_order_id`| string | **Yes** | From 2.9 |
| `gateway_payment_id`| string | **Yes** | Issued by Cashfree. |

**Sample Request**:
```json
{
  "gateway_order_id": "order_Fxy...",
  "gateway_payment_id": "pay_Fxy..."
}
```
**Sample Response (200 OK)**:
```json
{
  "status": "success",
  "data": {
    "message": "Payment verified successfully",
    "transaction_id": 1,
    "status": "completed",
    "new_balance": "850.00"
  }
}
```

### 2.11 Get Wallet Transactions
Get all wallet transactions for the authenticated user.
- **URL**: `/rider/wallet/transactions/`
- **Method**: `GET`
- **Auth Required**: Yes
- Supports Pagination (`page`, `page_size`).

**Sample Request**: `GET /rider/wallet/transactions/`
**Sample Response (200 OK)**:
```json
{
  "status": "success",
  "data": {
    "count": 5,
    "next": null,
    "previous": null,
    "results": [
      {
        "id": 1,
        "amount": "500.00",
        "txn_type": "credit",
        "status": "completed",
        "gateway_order_id": "order_Fxy...",
        "gateway_payment_id": "pay_Fxy...",
        "created_at": "2026-04-07T10:00:00Z"
      }
    ]
  }
}
```

### 2.12 Direct Wallet Payment
Initiate a direct payment from wallet without external gateway. Used for internal payments like trip payments.
- **URL**: `/rider/wallet/payment/`
- **Method**: `POST`
- **Auth Required**: Yes

**Parameters**:
| Name | Type | Required | Description |
| ---- | ---- | -------- | ----------- |
| `amount` | string | **Yes** | Amount to pay |
| `purpose` | string | No | Purpose of payment (default: "Payment") |
| `reference_id` | string | No | Reference ID for the payment (e.g., trip ID) |
| `idempotency_key` | string | No | Unique key to prevent duplicate payments |

**Sample Request**:
```json
{
  "amount": "100.00",
  "purpose": "Trip payment",
  "reference_id": "TRIP_123",
  "idempotency_key": "unique-key-123"
}
```
**Sample Response (201 Created)**:
```json
{
  "status": "success",
  "data": {
    "transaction_id": 123,
    "amount": "100.00",
    "new_balance": "250.00",
    "purpose": "Trip payment",
    "reference_id": "TRIP_123",
    "idempotency_key": "unique-key-123",
    "message": "Payment successful"
  }
}
```

---

## 3. Driver Functions (App: `driver`)

### 3.1 Get Driver Profile
- **URL**: `/driver/driver/profile/`
- **Method**: `GET`
- **Auth Required**: Yes (IsDriver)

**Sample Request**: `GET /driver/driver/profile/`
**Sample Response (200 OK)**:
```json
{
  "status": "success",
  "data": {
    "id": 1,
    "user": { "id": 2, "full_name": "Driver A" },
    "status": "offline",
    "approved": true,
    "license_doc": "/media/docs/l123.jpg",
    "license_expiry": "2028-10-10",
    "active_vehicle": 1,
    "ratings": "4.5"
  }
}
```

### 3.2 Update Driver Profile
- **URL**: `/driver/driver/update/`
- **Method**: `PATCH`
- **Auth Required**: Yes (IsDriver)

**Parameters (All Optional)**: `license_doc` (file), `license_expiry` (string YYYY-MM-DD), `active_vehicle` (int).

**Sample Request (JSON)**:
```json
{ "active_vehicle": 2 }
```
**Sample Response**: DriverProfile object (From 3.1).

### 3.3 List Driver Earnings
- **URL**: `/driver/earnings/`
- **Method**: `GET`
- **Auth Required**: Yes (IsDriver)
- Supports pagination.

**Sample Request**: `GET /driver/earnings/?page_size=10`
**Sample Response (200 OK)**:
```json
{
  "status": "success",
  "data": {
    "count": 10,
    "results": [
      {
        "id": 1,
        "trip_id": 101,
        "amount_collected": "200.00",
        "platform_fee": "20.00",
        "net_earning": "180.00",
        "created_at": "2026-04-07T10:00:00Z"
      }
    ]
  }
}
```

### 3.4 Driver Earnings Summary
- **URL**: `/driver/earnings/summary/`
- **Method**: `GET`
- **Auth Required**: Yes (IsDriver)

**Sample Request**: `GET /driver/earnings/summary/`
**Sample Response (200 OK)**:
```json
{
  "status": "success",
  "data": {
    "total_earned": "15000.00",
    "total_commission": "1500.00",
    "total_trips": 100,
    "today_earned": "350.00",
    "today_trips": 2,
    "commission_percent": 10
  }
}
```

### 3.5 List Default Vehicles
- **URL**: `/driver/vehicles/`
- **Method**: `GET`
- **Auth Required**: Yes (IsDriver)

**Sample Request**: `GET /driver/vehicles/`
**Sample Response (200 OK)**:
```json
{
  "status": "success",
  "data": [
    {
      "id": 1,
      "vehicle_number": "TS07AB1234",
      "vehicle_type": { "type": "sedan", "name": "Sedan" },
      "brand": "Maruti",
      "model": "Swift Dzire",
      "color": "White",
      "year": 2020,
      "capacity": 4,
      "status": "active"
    }
  ]
}
```

### 3.6 Create Vehicle
- **URL**: `/driver/vehicles/add/`
- **Method**: `POST`
- **Auth Required**: Yes (IsDriver)

**Parameters**:
| Name | Type | Required | Description |
| ---- | ---- | -------- | ----------- |
| `vehicle_number` | string | **Yes** | Registration Plate. |
| `vehicle_type` | string | **Yes** | e.g. `sedan`, `auto`, `suv`. |
| `brand`, `model`, `color` | string | No | Cosmetic details. |
| `year`, `capacity` | int | No | Vehicle age and seater. |
| `rc_doc`, `vehicle_pic` | file | No | Multipart file uploads. |

**Sample Request**:
```json
{
  "vehicle_number": "AP09XY8888",
  "vehicle_type": "suv",
  "brand": "Toyota",
  "model": "Innova",
  "color": "Silver",
  "year": 2023,
  "capacity": 6
}
```
**Sample Response (201 Created)**: (returns Vehicle object same as 3.5).

### 3.7 Update Vehicle
- **URL**: `/driver/vehicles/<vehicle_id>/`
- **Method**: `PATCH`
- **Auth Required**: Yes (IsDriver)

**Sample Request**:
```json
{ "color": "Matte Black" }
```
**Sample Response**: Updated vehicle object.

### 3.8 Delete Vehicle
- **URL**: `/driver/vehicles/<vehicle_id>/delete/`
- **Method**: `DELETE`
- **Auth Required**: Yes (IsDriver)

**Sample Request**: `DELETE /driver/vehicles/1/delete/`
**Sample Response (200 OK)**:
```json
{
  "status": "success",
  "data": { "message": "Vehicle deleted successfully" }
}
```

### 3.9 Admin: List Drivers
- **URL**: `/driver/admin/`
- **Method**: `GET`
- **Auth Required**: Yes (IsAdmin)
- Filters: `approved=true/false`, `status`.

**Sample Response**: Paginated Driver profiles.

### 3.10 Admin: Retrieve Driver
- **URL**: `/driver/admin/<driver_id>/`
- **Method**: `GET`
- **Auth Required**: Yes (IsAdmin)

**Sample Response**: Driver detail including user struct and vehicle relationships.

### 3.11 Admin: Update KYC Status
- **URL**: `/driver/admin/<driver_id>/update-kyc/`
- **Method**: `PATCH`
- **Auth Required**: Yes (IsAdmin)

**Parameters**: Optional `approved` (bool), `status` (string).
**Sample Request**: `{"approved": true}`

### 3.12 Admin: Delete Driver
- **URL**: `/driver/admin/<driver_id>/delete/`
- **Method**: `DELETE`
- **Auth Required**: Yes (IsAdmin)
**Sample Response**: `{"message": "Driver deleted successfully"}`

### 3.13 Admin: Get Driver Vehicles
- **URL**: `/driver/admin/<driver_id>/vehicles/`
- **Method**: `GET`
- **Auth Required**: Yes (IsAdmin)
**Sample Response**: Array of vehicle objects matching 3.5.

---

## 4. Rides & Trips (App: `ride`)

### 4.1 Estimate Fare
- **URL**: `/ride/estimate-fare/`
- **Method**: `POST`
- **Auth Required**: Yes

**Parameters**:
| Name | Type | Required | Description |
| ---- | ---- | -------- | ----------- |
| `pickup_lat`, `pickup_long` | float | **Yes** | Origination |
| `destination_lat`, `destination_long`| float | **Yes**| Terminus |
| `distance_km` | float | **Yes** | Distance path. |
| `duration_min`| float | **Yes** | Duration path. |
| `vehicle_type`| string| No | `sedan`, `auto`, etc. |

**Sample Request**:
```json
{
  "pickup_lat": 17.4, "pickup_long": 78.4,
  "destination_lat": 17.5, "destination_long": 78.5,
  "distance_km": 15.5, "duration_min": 45.0,
  "vehicle_type": "sedan"
}
```
**Sample Response (200 OK)**:
```json
{
  "status": "success",
  "data": {
    "estimated_fare": "345.50",
    "fare_breakdown": {
      "base_fare": "60.00",
      "distance_fare": "205.00",
      "time_fare": "80.50",
      "surge_multiplier": "1.0",
      "min_fare_applied": false
    },
    "vehicle_type": "sedan",
    "pricing_source": "system",
    "distance_km": 15.5,
    "duration_min": 45.0,
    "validated_km": 15.5,
    "validated_min": 45.0
  }
}
```

### 4.2 Ride History
- **URL**: `/ride/ride-history/`
- **Method**: `GET`
- **Auth Required**: Yes (Rider)
- Query parameters: `status`, `page`, `page_size`.

**Sample Request**: `GET /ride/ride-history/?status=completed`
**Sample Response**:
```json
{
  "status": "success",
  "data": {
    "count": 5,
    "results": [
      {
        "id": 101,
        "status": "completed",
        "estimated_fare": "345.50",
        "created_at": "..."
      }
    ]
  }
}
```

### 4.3 Driver History
- **URL**: `/ride/driver-history/`
- **Method**: `GET`
- **Auth Required**: Yes (IsDriver)

**Sample Response**: Paginated Trip objects.

### 4.4 Get Trip Details
Checks Redis cache explicitly for active real-time trips.
- **URL**: `/ride/trip/<trip_id>/`
- **Method**: `GET`
- **Auth Required**: Yes (Participant only)

**Sample Request**: `GET /ride/trip/101/`
**Sample Response (Cache Hit Context)**:
```json
{
  "status": "success",
  "data": {
    "trip_id": 101,
    "status": "in_progress",
    "rider_id": "1",
    "driver_id": "15",
    "pickup_lat": 17.4,
    "pickup_lng": 78.4,
    ...
    "source": "cache"
  }
}
```

### 4.5 Trip Driver Details via GET
Fetches full driver details for the trip. Evaluates the Redis cache first for extreme performance, then gracefully falls back to the Django DB.
- **URL**: `/ride/trip/<trip_id>/details/`
- **Method**: `GET`
- **Auth Required**: Yes

**Sample Request**: `GET /ride/trip/101/details/` (No Body)
**Sample Response (200 OK)**:
```json
{
  "status": "success",
  "data": {
    "id": 101,
    "status": "accepted",
    "driver_name": "Driver A",
    "driver_phone": "+919876543210",
    "driver_rating": "4.5",
    "vehicle_info": {
        "vehicle_number": "TS07AB1234",
        "brand": "Maruti",
        "model": "Swift Dzire",
        "color": "White"
    },
    "source": "cache"
  }
}
```

### 4.6 Rate Trip
Post completion review process.
- **URL**: `/ride/rate-trip/`
- **Method**: `POST`
- **Auth Required**: Yes

**Parameters**:
| Name | Type | Required | Description |
| ---- | ---- | -------- | ----------- |
| `trip_id` | int | **Yes** | Trip ID. |
| `score` | int | **Yes** | Value 1 to 5. |
| `comments`| string | No | String review. |

**Sample Request**:
```json
{
  "trip_id": 101,
  "score": 5,
  "comments": "Great ride."
}
```
**Sample Response (201 Created)**:
```json
{
  "status": "success",
  "data": {
    "rating_id": 1,
    "trip_id": 101,
    "score": 5,
    "comments": "Great ride.",
    "message": "Rating submitted successfully"
  }
}
```

### 4.7 Admin: Trips List
- **URL**: `/ride/admin/trips/`
- **Method**: `GET`
- **Auth Required**: Yes (IsAdmin)
- Filters: `status`, `driver_id`, `user_id`.

**Sample Response**: Paginated TripList payloads.

### 4.8 Admin: Live Locations
- **URL**: `/ride/admin/live-locations/`
- **Method**: `GET`
- **Auth Required**: Yes (IsAdmin)

**Sample Response**:
```json
{
  "status": "success",
  "data": {
    "drivers": ["driver:15:lat=17.4,lng=78.4", ...],
    "riders": ["rider:1:lat=17.5,lng=78.5", ...]
  }
}
```

---

## 5. Payments (App: `payments`)

### 5.1 Create Order
Initiates Cashfree backend order processing for an intended payment target.
- **URL**: `/payments/create-order/`
- **Method**: `POST`
- **Auth Required**: Yes

**Parameters**:
| Name | Type | Required | Description |
| ---- | ---- | -------- | ----------- |
| `trip_id` | int | **Yes** | Target completed trip id. |

**Sample Request**:
```json
{ "trip_id": 101 }
```
**Sample Response (201 Created)**:
```json
{
  "status": "success",
  "data": {
    "payment_id": 10,
    "gateway": "cashfree",
    "gateway_order_id": "order_Fxy12344",
    "amount": "245.50",
    "amount_paise": 24550,
    "currency": "INR",
    "trip_id": 101,
    "description": "Payment for Trip #101",
    "prefill": {
        "name": "Raja Kumar",
        "contact": "+919876543210",
        "email": "raja@example.com"
    },
    "cashfree_payment_session_id": "session_xyz...",
    "cashfree_app_id": "your_app_id",
    "order_token": "token_abc..."
  }
}
```

### 5.2 Verify Order Payment
Performs the secure signature validity check after Cashfree returns Success payload.
- **URL**: `/payments/verify/`
- **Method**: `POST`
- **Auth Required**: Yes

**Parameters**:
| Name | Type | Required | Description |
| ---- | ---- | -------- | ----------- |
| `gateway_order_id`| string | **Yes** | Sent from step 5.1 |
| `gateway_payment_id`| string | **Yes** | Issued by Cashfree. |
| `gateway_signature` | string | **Yes** | Generated hash signature. |
| `gateway` | string | No | Optional, defaults to cashfree. |

**Sample Request**:
```json
{
  "gateway_order_id": "order_Fxy12344",
  "gateway": "cashfree"
}
```
**Sample Response (200 OK)**:
```json
{
  "status": "success",
  "data": {
    "message": "Payment verified successfully",
    "payment_id": 10,
    "status": "completed",
    "amount": "245.50",
    "gateway": "cashfree"
  }
}
```

### 5.3 Payment Webhook
Handles Cashfree background payment updates. No JWT needed (uses signature).
- **URL**: `/payments/webhook/`
- **Method**: `POST`
- **Auth Required**: No (Verified via `x-webhook-signature` or `X-Cashfree-Signature` Headers).

**Sample Request Payload**: (Handled by provider server).
**Sample Response**: `{"status": "ok"}`

### 5.4 Payment History
Retrieve user's paginated logged transactions and payments constraints.
- **URL**: `/payments/history/`
- **Method**: `GET`
- **Auth Required**: Yes

**Sample Request**: `GET /payments/history/?status=completed`
**Sample Response (200 OK)**:
```json
{
  "status": "success",
  "data": {
    "count": 1,
    "next": null,
    "previous": null,
    "results": [
      {
        "id": 10,
        "trip_id": 101,
        "amount": "245.50",
        "method": "online",
        "status": "completed",
        "payment_gateway": "cashfree",
        "gateway_order_id": "order_Fxy12344",
        "gateway_payment_id": "pay_Fxy12344",
        "cashfree_order_id": "order_Fxy12344",
        "cashfree_payment_id": "pay_Fxy12344",
        "cashfree_payment_session_id": "session_xyz...",
        "created_at": "2026-04-07T12:00:00Z"
      }
    ]
  }
}
```

### 5.5 Refund Payment
Trigger refunds gracefully.
- **URL**: `/payments/refund/`
- **Method**: `POST`
- **Auth Required**: Yes

**Parameters**:
| Name | Type | Required | Description |
| ---- | ---- | -------- | ----------- |
| `trip_id` | int | **Yes** | Applicable towards online paid cancelled trips. |

**Sample Request**:
```json
{ "trip_id": 101 }
```
**Sample Response (200 OK)**:
```json
{
  "status": "success",
  "data": {
    "message": "Refund initiated successfully",
    "refund_id": "rfnd_xyz123",
    "amount": "245.50",
    "trip_id": 101
  }
}
```

### 5.6 Admin: List Payments
- **URL**: `/payments/admin/payments/`
- **Method**: `GET`
- **Auth Required**: Yes (IsAdmin)
- Fitlers: `status`, `method`

**Sample Response**: Paginated payment blocks including all users.

### 5.7 Admin: List Transactions
- **URL**: `/payments/admin/transactions/`
- **Method**: `GET`
- **Auth Required**: Yes (IsAdmin)
- Filters: `status`

**Sample Response**: Paginated TransactionHistory payloads.
