# SaaradhiGo Admin Web App: API Integration Guide

This document is for developers building the frontend of the SaaradhiGo Admin Web App. 

**Important Note on UI Templates:** 
The complete frontend HTML pages are already designed and present in the [SaaradhiGo-Admin GitHub repository](https://github.com/saiteja-saaradhigo/SaaradhiGo-Admin). Your primary task is to integrate these existing HTML templates within the Django project. You will use Django template tags to dynamically render data and connect these templates to the VahanGo backend.

The admin app is a **Django project acting strictly as a frontend presentation layer**. It does **not** use a local database. All data operations are performed by communicating with the VahanGo Backend APIs.

---

## 1. Environment & Architecture Setup

### A. Environment Variables
Do not hardcode the API base URL. Use environment variables (e.g. via `python-dotenv` or `django-environ`) in `settings.py`:
```python
# settings.py
import os
API_BASE_URL = os.environ.get('API_BASE_URL', 'https://dev.api.saaradhigo.in')
```

### B. Handling Static Files
The pre-designed HTML pages from GitHub rely on static assets (CSS, JS, images).
1. Place all assets in a `static` directory and configure `STATICFILES_DIRS` in `settings.py`.
2. In your HTML templates, replace standard paths with Django's static tag:
```html
{% load static %}
<link rel="stylesheet" href="{% static 'css/style.css' %}">
<img src="{% static 'images/logo.png' %}" alt="Logo">
```

### C. Architectural Rules
1. **No Django Models:** Do not create or migrate any local database models. 
2. **Session Storage for Auth:** Store the backend's JWT access token in the user's Django session upon login (`request.session['access_token']`).
3. **Authorization Header:** Every API call (except login) must include the header: `Authorization: Bearer <access_token>`.

---

## 2. Authentication & Login Flow

Before accessing any admin endpoints, you must capture the admin's token.

**Login View Example:**
```python
from django.shortcuts import render, redirect
import requests
from django.conf import settings

def login_view(request):
    if request.method == 'POST':
        phone = request.POST.get('phone_number')
        password = request.POST.get('password') # Or OTP depending on auth method
        
        # 1. Call Backend API
        response = requests.post(f"{settings.API_BASE_URL}/auth/login/", json={
            "phone_number": phone,
            "password": password
        })
        
        # 2. Handle Response
        if response.status_code == 200:
            data = response.json().get('data', {})
            # 3. Store the Token in Session
            request.session['access_token'] = data.get('token')
            return redirect('dashboard')
        else:
            error_msg = response.json().get('message', 'Login failed')
            return render(request, 'admin/login.html', {'error': error_msg})
            
    return render(request, 'admin/login.html')
```

---

## 3. API Client Utility Example

To avoid repeating the token extraction logic, create a utility file (e.g., `services/api_client.py`):

```python
import requests
from django.conf import settings

def get_headers(request):
    """Retrieve token from Django session and construct headers."""
    token = request.session.get('access_token')
    headers = { 'Content-Type': 'application/json' }
    if token:
        headers['Authorization'] = f'Bearer {token}'
    return headers

def api_get(request, endpoint, params=None):
    url = f"{settings.API_BASE_URL}{endpoint}"
    return requests.get(url, headers=get_headers(request), params=params)

def api_post(request, endpoint, data=None):
    url = f"{settings.API_BASE_URL}{endpoint}"
    return requests.post(url, headers=get_headers(request), json=data)

def api_patch(request, endpoint, data=None):
    url = f"{settings.API_BASE_URL}{endpoint}"
    return requests.patch(url, headers=get_headers(request), json=data)

def api_delete(request, endpoint):
    url = f"{settings.API_BASE_URL}{endpoint}"
    return requests.delete(url, headers=get_headers(request))
```

---

## 4. Handling Forms & API Responses

### Standard Backend Error Format
When an API request fails, the backend will return a standard error JSON format. Parse this to display errors to the user:
```json
{
  "code": "invalid_input",
  "message": "The provided data is invalid.",
  "issue": "Invalid format",
  "field": "phone_number"
}
```

### Form Submission Example (POST/PATCH)
Here is an example of intercepting a form submission from the UI (e.g., rejecting a withdrawal) and passing it to the backend via `api_post`.

**`views.py`**
```python
from django.shortcuts import redirect, render
from .services.api_client import api_post

def reject_withdrawal(request, withdrawal_id):
    if request.method == 'POST':
        # 1. Capture form data
        admin_notes = request.POST.get('admin_notes')
        
        # 2. Send to backend
        payload = {"admin_notes": admin_notes}
        response = api_post(request, f'/driver/admin/withdrawals/{withdrawal_id}/reject/', data=payload)
        
        # 3. Handle response
        if response.status_code == 200:
            return redirect('withdrawals_list')
        else:
            # Parse standard error format
            error_data = response.json()
            context = {'error': error_data.get('message', 'Failed to reject')}
            return render(request, 'admin/withdrawal_reject.html', context)
            
    return render(request, 'admin/withdrawal_reject.html')
```

---

## 5. View & Template Example (Fetching Data)

Here is how to fetch and render a list of data using the `api_get` client.

**`views.py`**
```python
from django.shortcuts import render
from .services.api_client import api_get

def list_drivers(request):
    response = api_get(request, '/driver/admin/')
    context = {}
    if response.status_code == 200:
        response_data = response.json()
        # Ensure you match the backend's {"status": "success", "data": { "results": [...] }} format
        context['drivers'] = response_data.get('data', {}).get('results', [])
    else:
        context['drivers'] = []
        context['error'] = "Failed to load drivers from backend."

    return render(request, 'admin/drivers_list.html', context)
```

**`admin/drivers_list.html`**
```html
{% extends 'base.html' %}
{% load static %}

{% block content %}
<h2>Driver Management</h2>

{% if error %}
    <div class="alert alert-danger">{{ error }}</div>
{% else %}
    <table class="table">
        <thead>
            <tr>
                <th>Name</th>
                <th>Status</th>
                <th>Actions</th>
            </tr>
        </thead>
        <tbody>
            {% for driver in drivers %}
            <tr>
                <td>{{ driver.user.full_name }}</td>
                <td>{{ driver.status }}</td>
                <td>
                    <a href="{% url 'driver_detail' driver.id %}" class="btn btn-sm btn-info">View</a>
                </td>
            </tr>
            {% endfor %}
        </tbody>
    </table>
{% endif %}
{% endblock %}
```

---

## 6. Admin API Reference

Below are the specific endpoints you will use for the Admin interface. 

### A. Authentication & Users
- **List Users:** 
  - `GET /auth/admin/users/`
  - *Query Params:* `role` (rider/driver/admin), `is_active` (bool), `page`, `page_size`

### B. Driver Management & KYC
- **List Drivers:** `GET /driver/admin/`
- **Retrieve Driver Details:** `GET /driver/admin/<driver_id>/`
- **Get Driver Vehicles:** `GET /driver/admin/<driver_id>/vehicles/`
- **Update Driver KYC Status:** 
  - `PATCH /driver/admin/<driver_id>/update-kyc/`
  - *Body:* `{"approved": true, "status": "active"}`
- **Delete Driver:** `DELETE /driver/admin/<driver_id>/delete/`

### C. Driver Withdrawals
- **List Withdrawals:** `GET /driver/admin/withdrawals/`
- **Approve Withdrawal:** 
  - `POST /driver/admin/withdrawals/<withdrawal_id>/approve/`
  - *Body:* `{"reason": "Approved manually"}`
- **Reject Withdrawal:** 
  - `POST /driver/admin/withdrawals/<withdrawal_id>/reject/`
  - *Body:* `{"admin_notes": "Reason for rejection"}`
- **Bulk Action Withdrawals:** 
  - `POST /driver/admin/withdrawals/bulk-action/`
  - *Body:* `{"action": "approve" | "reject", "withdrawal_ids": [1, 2], "admin_notes": "..."}`

### D. Rides & Monitoring
- **List Trips:** `GET /ride/admin/trips/`
- **Live Locations:** `GET /ride/admin/live-locations/`

### E. Payments & Transactions
- **List Payments:** `GET /payments/admin/payments/`
- **List Transactions:** `GET /payments/admin/transactions/`

---
*Note: Make sure to handle session expiration gracefully. If any API returns a 401 Unauthorized, redirect the user to the admin login page and clear `request.session['access_token']`.*
