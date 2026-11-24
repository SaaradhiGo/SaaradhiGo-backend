"""Readable references (owner, 24 Nov): city-coded, unguessable, never a database id."""
import re
from datetime import date

import pytest

from base import public_ref

pytestmark = pytest.mark.django_db


def test_shapes_and_alphabet():
    trip = public_ref.make(public_ref.TRIP, 'IN-TG-HYD', date(2026, 11, 16))
    assert re.fullmatch(r'HYD-261116-[ACDEFGHJKMNPQRTUVWXY34679]{4}', trip)
    assert re.fullmatch(r'D-TV-[A-Z0-9]{4}', public_ref.make(public_ref.DRIVER, 'US-ID-TV'))
    assert public_ref.make(public_ref.TICKET, 'IN-TG-HYD').startswith('SUP-HYD-')
    assert not set('0O1IL2Z5S8B') & set(public_ref.ALPHABET)


def test_parse_reads_new_references():
    assert public_ref.parse('hyd-261116-k7q4') == ('trip', 'HYD-261116-K7Q4')
    assert public_ref.parse('PO-HYD-M3XA') == ('payout', 'PO-HYD-M3XA')
    assert public_ref.parse('T-232') is None


def test_a_new_rider_gets_a_reference_from_their_city():
    from django.contrib.auth import get_user_model

    from servers.admin_access.models import GeoNode
    from servers.rider.models import Rider

    user = get_user_model().objects.create_user(phone_number='+919800000002', role='rider')
    rider = Rider.objects.filter(user_id=user).first() or Rider.objects.create(user_id=user)
    rider.refresh_from_db()
    assert rider.public_ref and rider.public_ref.startswith('R-')
    assert not rider.public_ref.endswith(str(rider.pk))
    assert GeoNode is not None
