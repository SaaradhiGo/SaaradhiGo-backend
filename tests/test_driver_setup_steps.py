"""Guided setup (10 Nov v2): six steps from the market's items, one status per file, upload times.

An uploaded, unreviewed file is "In review", never "Missing"; a step is the worst of its items;
a US driver's identity step has no Aadhaar or PAN; Continue setup opens the next unfinished step.
"""
import pytest

from finance_world import client_for
from servers.driver import requirements as req
from servers.driver.models import Driver, DriverDocumentReview
from test_driver_requirements import get, new_driver
from test_payout_settings_extension import india  # noqa: F401
from test_pricing_module import geo, world  # noqa: F401

pytestmark = [pytest.mark.django_db]


def _files(checklist, key):
    return {f['key']: f for i in checklist['items'] if i['key'] == key for f in i['files']}


def test_six_steps_and_a_us_identity_step_without_aadhaar(world):  # noqa: F811
    c = get(client_for(new_driver(world).user_id), 'requirements/')['checklist']
    assert [s['key'] for s in c['steps']] == ['personal', 'contact', 'identity', 'vehicle', 'vehicle_docs', 'payouts']
    assert c['steps_done'] == 0 and c['next_step'] == 'personal' and c['minutes_left'] > 0
    us = get(client_for(new_driver(world, calling='1', node='US-TX-AUS').user_id), 'requirements/')['checklist']
    identity = next(s for s in us['steps'] if s['key'] == 'identity')
    assert 'aadhaar' not in identity['items'] and 'pan' not in identity['items']


def test_an_uploaded_file_is_in_review_never_missing(world):  # noqa: F811
    d = new_driver(world)
    d.license_doc.name = 'license_docs/front.pdf'
    d.save()
    d.refresh_from_db()
    assert d.file_times.get('license_doc')
    c = req.checklist(d)
    files = _files(c, 'driving_licence')
    assert files['license_front']['status'] == 'in_review' and files['license_front']['uploaded_at']
    assert files['license_back']['status'] == 'missing'
    identity = next(s for s in c['steps'] if s['key'] == 'identity')
    assert identity['status'] == 'todo'          # the back and the number are still missing


def test_a_rejection_marks_the_step_for_action_with_the_reason(world):  # noqa: F811
    d = new_driver(world)
    d.police_verification_doc.name = 'police_verification_docs/p.pdf'
    d.save(update_fields=['police_verification_doc'])
    d.refresh_from_db()
    assert d.file_times.get('police_verification_doc')     # stamped on an update_fields save too
    DriverDocumentReview.objects.create(driver=d, document_key='police_verification', state='rejected',
                                        reason='Photo unclear', file_name='police_verification_docs/p.pdf')
    c = req.checklist(Driver.objects.get(pk=d.pk))
    f = _files(c, 'police_verification')['police_verification']
    assert f['status'] == 'rejected' and f['reason'] == 'Photo unclear'
    assert next(s for s in c['steps'] if s['key'] == 'identity')['status'] == 'action'


def test_step_status_is_the_worst_of_its_items():
    item = lambda s, need='required': {'status': s, 'need': need}  # noqa: E731
    assert req.step_status([item('approved'), item('in_review')]) == 'in_review'
    assert req.step_status([item('in_review'), item('missing')]) == 'todo'
    assert req.step_status([item('missing'), item('rejected')]) == 'action'
    assert req.step_status([item('done'), item('missing', 'optional')]) == 'done'
    steps = [{'key': 'a', 'status': 'done'}, {'key': 'b', 'status': 'in_review'}, {'key': 'c', 'status': 'todo'}]
    assert req.next_step(steps) == 'c'
    assert req.next_step(steps[:2]) == 'b'
