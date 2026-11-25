"""25 Nov (owner), console capabilities, Configure: archive a place that is not a planned market,
through a second person, never a live city."""

import pytest

from servers.admin_access.models import ACTIVE, DRAFT, GeoNode
from test_sos_support_zones_matrix import geo, operator  # noqa: F401

pytestmark = pytest.mark.django_db
URL = '/api/v1/platform/admin/markets/{}/status/'


def test_archiving_a_draft_city_is_a_request_for_a_second_person(geo):
    from io import StringIO

    from django.core.management import call_command

    from conftest import seed_actor
    from servers.approvals.models import ApprovalRequest

    call_command('seed_approval_policies', actor_email=seed_actor().email, stdout=StringIO())

    GeoNode.objects.filter(code='IN-TG-HYD').update(status=DRAFT)
    c, _ = operator('super_admin', geo)
    r = c.post(URL.format('IN-TG-HYD'), {'status': 'archived', 'reason': 'Not a planned market'}, format='json')
    assert r.status_code in (200, 201, 202), r.content
    row = ApprovalRequest.objects.filter(action_key='platform.city_status').order_by('-id').first()
    assert row is not None and row.payload['to_status'] == 'archived'


def test_a_live_city_is_paused_first(geo):
    GeoNode.objects.filter(code='IN-TG-HYD').update(status=ACTIVE)
    c, _ = operator('super_admin', geo)
    r = c.post(URL.format('IN-TG-HYD'), {'status': 'archived', 'reason': 'Not a planned market'}, format='json')
    assert r.status_code == 409 and r.json()['error']['code'] == 'NOT_ARCHIVABLE'


def test_city_ops_cannot_archive(geo):
    GeoNode.objects.filter(code='IN-TG-HYD').update(status=DRAFT)
    c, _ = operator('city_ops_manager', geo)
    r = c.post(URL.format('IN-TG-HYD'), {'status': 'archived', 'reason': 'Not a planned market'}, format='json')
    assert r.status_code in (403, 404)
