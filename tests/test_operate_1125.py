"""25 Nov (owner), console capabilities, Operate: a ticket from a trip, a trip linked to a ticket,
trips by plate, the call log on a trip, and staff exports and receipt resends in the audit log."""

from decimal import Decimal

import pytest
from django.utils import timezone

from finance_world import zone
from notifications_support import geo  # noqa: F401
from servers.admin_audit.models import AdminAuditLog
from test_sos_support_zones_matrix import operator

pytestmark = pytest.mark.django_db
TRIPS = '/api/v1/ride/admin/trips'


@pytest.fixture
def trip(geo, auth_client_rider, auth_client_driver):  # noqa: F811
    from servers.ride.models import Trip, TripStatus

    _, rider = auth_client_rider
    _, driver_user = auth_client_driver
    from servers.driver.models import Vehicle, VehicleType

    vtype, _ = VehicleType.objects.get_or_create(type='sedan')
    vehicle = Vehicle.objects.create(driver_id=driver_user.driver, vehicle_type_id=vtype, vehicle_number='TS09AB1234')
    done, _ = TripStatus.objects.get_or_create(status_code='completed')
    t = Trip.objects.create(
        zone=zone('IN-TG-HYD', geo['IN-TG-HYD']), user_id=rider, driver_id=driver_user.driver, status_id=done,
        vehicle_id=vehicle, pickup_lat=Decimal('17.385'), pickup_long=Decimal('78.486'),
        destination_lat=Decimal('17.44'), destination_long=Decimal('78.38'),
        estimated_fare=Decimal('200.00'), accepted_at=timezone.now(), payment_method='cash', payment_status='paid')
    t.refresh_from_db()
    return t


class TestTicketFromTrip:
    def test_support_opens_a_ticket_for_the_rider_linked_to_the_trip(self, geo, trip):
        from servers.support.models import SupportMessage, SupportTicket

        c, agent = operator('support_agent', geo)
        r = c.post(f'{TRIPS}/{trip.id}/ticket/', {'for': 'rider', 'issue_type': 'fare_dispute',
                                                   'description': 'Rider called about the fare on this trip.'}, format='json')
        assert r.status_code == 201, r.content
        t = SupportTicket.objects.get(pk=r.json()['data']['ticket_id'])
        assert t.trip_id_id == trip.id and t.user_id_id == trip.user_id_id and t.assigned_to_id == agent.id
        # The person did not write it: the first message is an internal note, never shown to them.
        assert SupportMessage.everything.get(ticket=t).author_role == 'internal'
        assert t.messages.count() == 0
        # What the rider sees as the ticket's opening line is not what staff wrote.
        assert 'fare' not in (t.description or '') and t.description.startswith('Support opened this')
        assert AdminAuditLog.objects.filter(action='support_ticket_opened_from_trip', target_id=str(trip.id)).exists()

    def test_for_the_driver_and_refusals(self, geo, trip):
        c, _ = operator('support_agent', geo)
        assert c.post(f'{TRIPS}/{trip.id}/ticket/', {'for': 'driver', 'description': 'short'}, format='json').status_code == 400
        assert c.post(f'{TRIPS}/{trip.id}/ticket/', {'for': 'nobody', 'description': 'Long enough text here'},
                      format='json').status_code == 400
        ok = c.post(f'{TRIPS}/{trip.id}/ticket/', {'for': 'driver', 'description': 'Driver asked about this trip.'}, format='json')
        assert ok.status_code == 201

    def test_another_city_cannot_see_the_trip(self, geo, trip):
        c, _ = operator('support_agent', geo, 'US-ID-TV') if 'US-ID-TV' in geo else operator('support_agent', geo, 'IN-AP')
        assert c.post(f'{TRIPS}/{trip.id}/ticket/', {'for': 'rider', 'description': 'Long enough text here'},
                      format='json').status_code == 404


class TestLinkTrip:
    def test_links_only_this_persons_trip_by_reference(self, geo, trip, auth_client_rider):
        from servers.support.models import SupportTicket

        _, rider = auth_client_rider
        ticket = SupportTicket.objects.create(user_id=rider, issue_type='payment', description='Charged twice')
        c, _ = operator('support_agent', geo)
        ref = trip.public_ref or str(trip.id)
        r = c.post(f'/api/v1/support/admin/tickets/{ticket.id}/trip/', {'trip': ref}, format='json')
        assert r.status_code == 200, r.content
        ticket.refresh_from_db()
        assert ticket.trip_id_id == trip.id
        assert AdminAuditLog.objects.filter(action='support_ticket_trip_linked').exists()
        assert c.post(f'/api/v1/support/admin/tickets/{ticket.id}/trip/', {'trip': 'HYD-000000-ZZZZ'},
                      format='json').status_code == 400

    def test_refuses_a_trip_of_someone_else(self, geo, trip):
        from notifications_support import make_rider_user
        from servers.support.models import SupportTicket

        other = make_rider_user(geo)
        ticket = SupportTicket.objects.create(user_id=other, issue_type='payment', description='Charged twice')
        c, _ = operator('support_agent', geo)
        r = c.post(f'/api/v1/support/admin/tickets/{ticket.id}/trip/', {'trip': str(trip.id)}, format='json')
        assert r.status_code == 400 and r.json()['error']['code'] == 'NOT_THEIR_TRIP'


class TestTripsListAndDetail:
    def test_plate_search_in_any_spacing(self, geo, trip):
        c, _ = operator('support_agent', geo)
        ids = [row['id'] for row in c.get(f'{TRIPS}/?plate=ts 09-ab 1234').json()['data']['results']]
        assert trip.id in ids
        assert c.get(f'{TRIPS}/?plate=KA01').json()['data']['results'] == []

    def test_the_detail_has_the_call_log_without_numbers(self, geo, trip):
        from datetime import timedelta

        from servers.callbridge.models import CallSession

        now = timezone.now()
        CallSession.objects.create(trip=trip, from_role='rider', started_at=now, expires_at=now + timedelta(minutes=15))
        c, _ = operator('support_agent', geo)
        data = c.get(f'{TRIPS}/{trip.id}/').json()['data']
        assert data['calls'][0]['from_role'] == 'rider'
        assert 'number' not in data['calls'][0]

    def test_an_export_is_recorded(self, geo):
        c, _ = operator('support_agent', geo)
        r = c.post(f'{TRIPS}/export-log/', {'rows': 12, 'view': 'all', 'filters': {'status': 'completed'}}, format='json')
        assert r.status_code == 201
        row = AdminAuditLog.objects.get(action='trips_exported')
        assert row.after['rows'] == 12 and row.after['filters'] == {'status': 'completed'}
