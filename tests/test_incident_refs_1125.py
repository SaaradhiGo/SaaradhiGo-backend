"""Safety incidents carry a readable reference like every other record (owner, 25 Nov)."""
import re

import pytest

from base import public_ref, refs

pytestmark = pytest.mark.django_db


def test_incident_shape_and_parse():
    ref = public_ref.make(public_ref.INCIDENT, 'IN-TG-HYD')
    assert re.fullmatch(r'INC-HYD-[ACDEFGHJKMNPQRTUVWXY34679]{4}', ref)
    assert public_ref.parse(ref.lower()) == ('incident', ref)
    # The old sequential form still reads through base.refs, never as a new reference.
    assert public_ref.parse('INC-13') is None
    assert refs.parse('INC-13') == ('incident', 13)


def test_a_new_incident_gets_its_reference_and_shows_it():
    from servers.safety.services import open_incident

    incident, created = open_incident(source='manual', category='other', severity='high',
                                      title='Test case')
    assert created
    incident.refresh_from_db()
    assert incident.public_ref and incident.public_ref.startswith('INC-')
    assert refs.incident_ref(incident) == incident.public_ref
    assert not incident.public_ref.endswith(f'-{incident.pk}')
