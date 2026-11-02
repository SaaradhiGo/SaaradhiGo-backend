"""Driver KYC documents: on demand, scoped, five minutes, audited (AUDIT 27.78).

Before: the detail, the KYC queue (a list) and the driver's own responses all
embedded a 15-minute presigned link to every document, the masking renderer
let `pii.view` held in ANY city unmask documents of another city, and no
document view was recorded. These tests fail on that code and pass now.
"""

import json
import logging
import time
from unittest import mock

import pytest

from servers.admin_audit.models import AdminAuditLog
from servers.driver import document_access
from servers.driver.models import Driver, Vehicle
from test_drivers_module import url, world  # noqa: F401 - fixtures
from test_sos_support_zones_matrix import geo, operator  # noqa: F401 - fixtures

pytestmark = pytest.mark.django_db

SIGNED_HOST = 'https://private-docs.test/'


class SigningStorage:
    """Stands in for PrivateDocumentStorage: records the expiry it was asked for."""

    querystring_auth = True
    custom_domain = None

    def __init__(self):
        self.expiries = []

    def url(self, name, expire=None):
        self.expiries.append(expire)
        return f'{SIGNED_HOST}{name}?X-Amz-Expires={expire}&X-Amz-Signature=sig'


@pytest.fixture(autouse=True)
def storage(monkeypatch):
    fake = SigningStorage()
    for model, columns in ((Driver, document_access.DRIVER_COLUMNS),
                           (Vehicle, document_access.VEHICLE_COLUMNS)):
        for column in columns:
            monkeypatch.setattr(model._meta.get_field(column), 'storage', fake, raising=False)
    return fake


def _view(client, driver, key, vehicle_id=None):
    suffix = f'documents/{key}/view/' + (f'?vehicle_id={vehicle_id}' if vehicle_id else '')
    return client.get(url(driver, suffix))


def _views(driver):
    return AdminAuditLog.objects.filter(action=document_access.VIEWED_ACTION,
                                        target_type='driver', target_id=str(driver.id))


class TestTtl:
    def test_a_document_url_lives_at_most_five_minutes(self, settings, storage, world):
        settings.DOCUMENT_URL_TTL_SECONDS = 900
        assert document_access.url_ttl() == 300
        document_access.signed_url(world['review'].license_doc)
        assert storage.expiries == [300]

    def test_the_operator_endpoint_says_so(self, geo, world, storage):
        client, _ = operator('city_ops_manager', geo)
        r = _view(client, world['review'], 'license_front')
        assert r.status_code == 200, r.content
        assert r.json()['data']['expires_in'] <= 300
        assert storage.expiries and all(e <= 300 for e in storage.expiries)

    def test_an_unsigned_storage_gives_no_url(self, world, monkeypatch):
        class Public(SigningStorage):
            querystring_auth = False
        monkeypatch.setattr(Driver._meta.get_field('license_doc'), 'storage', Public(),
                            raising=False)
        assert document_access.signed_url(Driver.objects.get(pk=world['review'].pk)
                                          .license_doc) is None


class TestScope:
    def test_an_out_of_scope_operator_gets_404(self, geo, world):
        austin, _ = operator('city_ops_manager', geo, scope_code='US-TX-AUS')
        assert _view(austin, world['review'], 'license_front').status_code == 404
        assert not _views(world['review']).exists()

    def test_in_scope_without_pii_view_gets_403(self, geo, world):
        analyst, _ = operator('analyst', geo)
        assert _view(analyst, world['review'], 'license_front').status_code == 403
        assert not _views(world['review']).exists()

    def test_pii_view_in_another_city_does_not_open_this_citys_documents(self, geo, world):
        # Hyderabad pii.view, Austin driver: out of scope, 404.
        hyd, _ = operator('city_ops_manager', geo)
        assert _view(hyd, world['aus'], 'license_front').status_code == 404

    def test_a_vehicle_document_needs_that_drivers_vehicle(self, geo, world):
        client, _ = operator('city_ops_manager', geo)
        other_vehicle = Vehicle.objects.filter(driver_id=world['older']).first()
        assert _view(client, world['review'], 'rc', other_vehicle.id).status_code == 404
        own = Vehicle.objects.filter(driver_id=world['review']).first()
        assert _view(client, world['review'], 'rc', own.id).status_code == 200


class TestEveryViewIsAudited:
    def test_a_view_writes_one_row_naming_who_and_what_and_no_url(self, geo, world):
        client, user = operator('city_ops_manager', geo)
        r = _view(client, world['review'], 'license_back')
        assert r.status_code == 200
        rows = list(_views(world['review']))
        assert len(rows) == 1
        row = rows[0]
        assert row.actor_id == user.id
        assert row.after['document_key'] == 'license_back'
        assert row.after['column'] == 'license_doc_back'
        assert row.created_at is not None
        dumped = json.dumps([row.before, row.after, row.reason])
        assert 'http' not in dumped and SIGNED_HOST not in dumped

    def test_a_failed_audit_write_opens_nothing(self, geo, world):
        client, _ = operator('city_ops_manager', geo)
        with mock.patch('servers.admin_audit.services.AdminAuditLog.objects.create',
                        side_effect=RuntimeError('db down')):
            r = _view(client, world['review'], 'license_front')
        assert r.status_code == 503
        assert SIGNED_HOST not in r.content.decode()


class TestNoUrlsInResponses:
    def test_list_queue_detail_and_vehicle_endpoints_carry_no_document_url(self, geo, world):
        client, _ = operator('city_ops_manager', geo)
        driver = world['review']
        for path in ('/api/v1/driver/admin/', '/api/v1/driver/admin/kyc-queue/',
                     url(driver), url(driver, 'full/'), url(driver, 'vehicles/')):
            r = client.get(path)
            assert r.status_code == 200, (path, r.content)
            body = r.content.decode()
            assert SIGNED_HOST not in body, path
            assert 'X-Amz' not in body, path
            assert '_doc_url' not in body, path

    def test_the_detail_says_which_documents_may_be_opened(self, geo, world):
        client, _ = operator('city_ops_manager', geo)
        docs = client.get(url(world['review'], 'full/')).json()['data']['documents']
        assert any(d['viewable'] for d in docs)
        assert all('url' not in d for d in docs)

    def test_the_drivers_own_responses_carry_view_links_not_bucket_urls(self, world, storage):
        from rest_framework.test import APIClient
        from rest_framework_simplejwt.tokens import AccessToken

        driver = world['review']
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f'Bearer {AccessToken.for_user(driver.user_id)}')
        profile = client.get('/api/v1/driver/driver/profile/').json()['data']
        assert '/api/v1/driver/driver/documents/view/' in profile['license_doc']
        assert '/documents/view/' in profile['active_vehicle_details']['rc_doc']
        listed = client.get('/api/v1/driver/vehicles/')
        body = listed.content.decode()
        assert SIGNED_HOST not in body and '/documents/view/' not in body
        assert SIGNED_HOST not in json.dumps(profile)
        assert storage.expiries == []  # nothing was signed until somebody opens one


class TestTheDriversOwnViewLink:
    def _link(self, driver):
        from rest_framework.test import APIClient
        from rest_framework_simplejwt.tokens import AccessToken

        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f'Bearer {AccessToken.for_user(driver.user_id)}')
        link = client.get('/api/v1/driver/driver/profile/').json()['data']['license_doc']
        return link[link.index('/api/v1/'):]

    def test_opening_it_audits_and_redirects_to_a_five_minute_url(self, world, storage):
        from rest_framework.test import APIClient

        driver = world['review']
        r = APIClient().get(self._link(driver))
        assert r.status_code == 302
        assert r['Location'].startswith(SIGNED_HOST)
        assert storage.expiries == [300]
        row = _views(driver).get()
        assert row.actor_id == driver.user_id_id
        assert row.after['via'] == 'driver_self'
        assert SIGNED_HOST not in json.dumps(row.after)

    def test_an_expired_or_tampered_link_opens_nothing(self, world):
        from rest_framework.test import APIClient

        link = self._link(world['review'])
        later = time.time() + document_access.url_ttl() + 5
        with mock.patch('django.core.signing.time.time', return_value=later):
            assert APIClient().get(link).status_code == 404
        tampered = link.replace('/view/', '/view/x')
        assert APIClient().get(tampered).status_code == 404
        assert not _views(world['review']).exists()

    def test_a_deactivated_driver_cannot_open_it(self, world):
        from rest_framework.test import APIClient

        driver = world['review']
        link = self._link(driver)
        type(driver.user_id).objects.filter(pk=driver.user_id_id).update(is_active=False)
        assert APIClient().get(link).status_code == 404


class TestLogsCarryNoUrl:
    def test_no_log_record_contains_a_document_url(self, geo, world, caplog):
        from rest_framework.test import APIClient

        caplog.set_level(logging.DEBUG)
        client, _ = operator('city_ops_manager', geo)
        _view(client, world['review'], 'license_front')
        TestTheDriversOwnViewLink()._link(world['review'])
        APIClient().get(TestTheDriversOwnViewLink()._link(world['review']))
        for record in caplog.records:
            message = record.getMessage()
            assert SIGNED_HOST not in message and 'X-Amz' not in message, message

    def test_no_logger_call_in_the_upload_and_document_code_names_a_url_or_key(self):
        # A static guard on the paths that handle document links and keys.
        import re
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        for rel in ('base/uploads_views.py', 'servers/driver/document_access.py',
                    'servers/driver/admin_drivers.py', 'servers/admin_access/reveal.py',
                    'integrations/storage/s3.py'):
            source = (root / rel).read_text(encoding='utf-8')
            for call in re.findall(r"logger\.\w+\((.*?)\)\s*$", source, flags=re.S | re.M):
                assert not re.search(r"\burl\b|\.url\b|upload_url|\['key'\]|\bkey=", call), (rel, call)
