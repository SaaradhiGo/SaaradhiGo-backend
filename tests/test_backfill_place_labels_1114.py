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
