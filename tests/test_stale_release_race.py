"""Two operators releasing the same stale trip at the same instant, on PostgreSQL.

`SELECT FOR UPDATE` is a no-op on SQLite, so the default suite can only prove that a
SECOND release is refused (`test_stale_trip_actions.TestTwoOperators`). This proves the
row lock: two simultaneous requests, one cancellation. Marked `postgres`; the dedicated
CI job runs it with `pytest -m postgres`.
"""

import threading
from datetime import timedelta

import pytest
from django.db import connection, connections
from django.utils import timezone

from servers.admin_audit.models import AdminAuditLog
from servers.ride.models import Trip
from test_sos_support_zones_matrix import (  # noqa: F401 - fixtures
    _client_for, _driver, _rider, _zone, geo, operator,
)
from test_trips_module import _trip

pytestmark = [pytest.mark.postgres, pytest.mark.django_db(transaction=True)]


def test_two_simultaneous_releases_cancel_the_trip_once(geo):
    if connection.vendor != 'postgresql':
        pytest.skip(f'needs PostgreSQL, got {connection.vendor!r}')
    now = timezone.now()
    hyd = _zone('IN-TG-HYD', geo['IN-TG-HYD'])
    rider = _rider('+919876511101', 'Lakshmi Devi')
    driver = _driver('+919876511102', 'Kiran Rao', geo['IN-TG-HYD'])
    trip = _trip(rider, hyd, status='accepted', driver=driver, fare=5_300,
                 requested_at=now - timedelta(minutes=45),
                 accepted_at=now - timedelta(minutes=40),
                 last_driver_activity_at=now - timedelta(minutes=30),
                 stale_flagged_at=now - timedelta(minutes=5), stale_reason='driver_silent_long')
    _, first = operator('city_ops_manager', geo)
    _, second = operator('city_ops_manager', geo)

    barrier = threading.Barrier(2)
    statuses = []

    def release(user):
        try:
            client = _client_for(user)
            barrier.wait(timeout=10)
            response = client.post(
                f'/api/v1/ride/admin/trips/{trip.id}/stale/release-driver/',
                {'reason': 'Driver is unreachable, ghost trip'}, format='json')
            statuses.append((response.status_code, response.content[:300]))
        finally:
            connections.close_all()

    threads = [threading.Thread(target=release, args=(u,)) for u in (first, second)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert sorted(code for code, _ in statuses) == [200, 409], statuses
    trip = Trip.objects.get(id=trip.id)
    assert trip.status_id.status_code == 'cancelled'
    assert AdminAuditLog.objects.filter(action='trip_cancelled', target_id=str(trip.id)).count() == 1
    assert AdminAuditLog.objects.filter(
        action='trip_stale_driver_released', target_id=str(trip.id)).count() == 1
