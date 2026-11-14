"""14 Nov (owner): trips labelled "Current location" get a short place name from their coordinates,
one cached lookup per point, and a later save never puts "Current location" back."""
from io import StringIO
from unittest import mock

import pytest
from django.core.management import call_command

from servers.ride.models import Trip


@pytest.mark.django_db
def test_backfill_labels_from_coordinates(auth_client_rider):
    _, user = auth_client_rider
    t = Trip.objects.create(user_id=user, pickup_address='Current location', destination_address='Charminar, Ghansi Bazaar, Hyderabad, Telangana, India',
                            pickup_lat=17.44, pickup_long=78.38, destination_lat=17.36, destination_long=78.47)
    assert t.pickup_label == 'Current location'
    addr = 'Cyber Towers, HITEC City, Hyderabad, Telangana 500081, India'
    with mock.patch('servers.ride.management.commands.backfill_place_labels.street_address', return_value=addr) as look:
        out = StringIO()
        call_command('backfill_place_labels', stdout=out)
    assert look.call_count == 1  # only the generic side is looked up
    t.refresh_from_db()
    assert t.pickup_label == 'Cyber Towers, HITEC City'
    assert t.pickup_address == 'Current location'  # the record keeps what was stored
    assert t.destination_label == 'Charminar, Ghansi Bazaar'
    assert 'fixed 1 trips' in out.getvalue()
    t.save()  # a full save later keeps the found label
    t.refresh_from_db()
    assert t.pickup_label == 'Cyber Towers, HITEC City'


@pytest.mark.django_db
def test_dry_run_changes_nothing(auth_client_rider):
    _, user = auth_client_rider
    t = Trip.objects.create(user_id=user, pickup_address='Current location', destination_address='x', pickup_lat=1, pickup_long=2,
                            destination_lat=1, destination_long=2)
    with mock.patch('servers.ride.management.commands.backfill_place_labels.street_address', return_value='Somewhere, Area, City'):
        out = StringIO()
        call_command('backfill_place_labels', '--dry-run', stdout=out)
    t.refresh_from_db()
    assert t.pickup_label == 'Current location'
    assert 'would fix 1 trips' in out.getvalue()


def test_a_house_number_is_not_the_place_name():
    from servers.ride.places import short_place
    assert short_place('10, Hitech City Rd, HITEC City, Hyderabad, Telangana 500081, India') == 'Hitech City Rd, HITEC City'
    assert short_place('2, Madhapur, Hyderabad, Telangana, India') == 'Madhapur, Hyderabad'
    assert short_place('Plot No 12, Kondapur, Hyderabad') == 'Kondapur, Hyderabad'
    assert short_place('7-1-28, Ameerpet, Hyderabad, Telangana 500016, India') == 'Ameerpet, Hyderabad'
    assert short_place('1600 Broadway, New York, NY 10019, USA') == '1600 Broadway, New York'


@pytest.mark.django_db
def test_backfill_redoes_numbered_labels(auth_client_rider):
    _, user = auth_client_rider
    t = Trip.objects.create(user_id=user, pickup_address='Current location', destination_address='x',
                            pickup_lat=17.44, pickup_long=78.38, destination_lat=1, destination_long=2)
    Trip.objects.filter(pk=t.pk).update(pickup_label='10, HITEC City')
    addr = '10, Hitech City Rd, HITEC City, Hyderabad, Telangana 500081, India'
    with mock.patch('servers.ride.management.commands.backfill_place_labels.street_address', return_value=addr):
        call_command('backfill_place_labels', stdout=StringIO())
    t.refresh_from_db()
    assert t.pickup_label == 'Hitech City Rd, HITEC City'
