"""Each document on the driver's checklist says where it stands (2026-11-06).

Read from the operators' per-document reviews, by the console's own rule: a
review counts only for the file it was about, so a re-upload is back to
`in_review`. The reason is the reviewer's text on a rejection.
"""

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from servers.driver.models import Driver, DriverDocumentReview

User = get_user_model()

pytestmark = pytest.mark.django_db

URL = '/api/v1/driver/driver/onboarding/'


@pytest.fixture
def driver():
    user = User.objects.create_user(phone_number='+919800000851', role='driver')
    return Driver.objects.create(user_id=user, approved=False, status='off')


def _docs(driver):
    client = APIClient()
    client.force_authenticate(user=driver.user_id)
    body = client.get(URL).json()['data']
    return {d['key']: d for d in body['driver_documents']}


def test_no_file_is_missing(driver):
    docs = _docs(driver)
    assert docs['license_front']['status'] == 'missing'
    assert docs['license_front']['reason'] is None


def test_an_unreviewed_file_is_in_review(driver):
    driver.license_doc = 'license_docs/front-1.jpg'
    driver.save(update_fields=['license_doc'])
    assert _docs(driver)['license_front']['status'] == 'in_review'


def test_a_rejection_carries_the_reviewers_reason(driver):
    driver.license_doc = 'license_docs/front-1.jpg'
    driver.save(update_fields=['license_doc'])
    DriverDocumentReview.objects.create(
        driver=driver, document_key='license_front', file_name='license_docs/front-1.jpg',
        state='rejected', reason='We could not read the licence number. Retake in daylight.')
    doc = _docs(driver)['license_front']
    assert doc['status'] == 'rejected'
    assert doc['reason'] == 'We could not read the licence number. Retake in daylight.'


def test_an_approval_has_no_reason(driver):
    driver.license_doc = 'license_docs/front-1.jpg'
    driver.save(update_fields=['license_doc'])
    DriverDocumentReview.objects.create(
        driver=driver, document_key='license_front', file_name='license_docs/front-1.jpg',
        state='approved', reason='')
    doc = _docs(driver)['license_front']
    assert doc['status'] == 'approved'
    assert doc['reason'] is None


def test_a_reupload_after_a_rejection_is_back_in_review(driver):
    driver.license_doc = 'license_docs/front-1.jpg'
    driver.save(update_fields=['license_doc'])
    DriverDocumentReview.objects.create(
        driver=driver, document_key='license_front', file_name='license_docs/front-1.jpg',
        state='rejected', reason='Blurred')
    driver.license_doc = 'license_docs/front-2.jpg'
    driver.save(update_fields=['license_doc'])
    doc = _docs(driver)['license_front']
    assert doc['status'] == 'in_review'
    assert doc['reason'] is None
