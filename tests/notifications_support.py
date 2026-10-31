"""Shared fixtures for the notifications tests (module 11).

NO NETWORK, NO CREDENTIALS. `block_the_network` (autouse in every notifications test file)
makes any attempt to open an HTTP connection fail the test: the adapters are exercised only
through mocked `requests` / `boto3` / `firebase_admin` calls. Fake secrets are obviously fake
("test-webhook-secret") and low-entropy so the secret scan stays quiet.
"""

from itertools import count
from types import SimpleNamespace

import pytest
from django.contrib.auth import get_user_model

from servers.admin_access.models import GeoNode
from servers.driver.models import Driver
from servers.notifications.models import (
    ACTIVE, NotificationTemplate, TemplateRegistration,
)
from servers.rider.models import Rider

User = get_user_model()
_n = count(1)

FAKE = {
    'MSG91_AUTH_KEY': 'test-msg91-key',
    'MSG91_WEBHOOK_SECRET': 'test-webhook-secret',
    'TWILIO_ACCOUNT_SID': 'ACtest',
    'TWILIO_AUTH_TOKEN': 'test-twilio-token',
    'TWILIO_FROM_NUMBER': '+15005550006',
    'SES_AWS_ACCESS_KEY_ID': 'test-ses-id',
    'SES_AWS_SECRET_ACCESS_KEY': 'test-ses-secret',
    'SES_AWS_REGION': 'us-east-1',
    'SES_FROM_EMAIL': 'noreply@example.test',
}


@pytest.fixture(autouse=True)
def block_the_network(monkeypatch):
    """Any real HTTP connection fails the test. (requests and botocore both use urllib3.)"""
    import urllib3.connectionpool

    def refuse(self, *args, **kwargs):  # pragma: no cover - only fires on a defect
        raise AssertionError('a notifications test tried to open a real network connection')

    monkeypatch.setattr(urllib3.connectionpool.HTTPConnectionPool, 'urlopen', refuse)


@pytest.fixture
def fake_credentials(settings):
    for name, value in FAKE.items():
        setattr(settings, name, value)
    settings.FIREBASE_CREDENTIALS_JSON = '{"fake": true}'
    return FAKE


@pytest.fixture
def geo(db):
    from conftest import seed_actor, seed_admin_access
    from io import StringIO
    from django.core.management import call_command

    seed_admin_access()
    call_command('seed_approval_policies', actor_email=seed_actor().email, stdout=StringIO())
    return {n.code: n for n in GeoNode.objects.all()}


def make_user(role, phone=None, email=None, name=None):
    i = next(_n)
    phone = phone or f'+9197000{i:05d}'
    return User.objects.create_user(
        phone_number=phone, username=phone, email=email, full_name=name or f'Person {i}',
        role=role)


def make_driver_user(geo, node='IN-TG-HYD', phone=None, email=None, approved=True):
    user = make_user('driver', phone, email)
    Driver.objects.create(user_id=user, home_city=geo[node], approved=approved,
                          doc_status='approved' if approved else 'pending', status='online')
    return user


def make_rider_user(geo, node='IN-TG-HYD', phone=None, email=None):
    user = make_user('rider', phone, email)
    Rider.objects.create(user_id=user, home_city=geo[node] if node else None)
    return user


def make_template(geo, key='test.event', channel='sms', country='IN', *, status=ACTIVE,
                  category='transactional', body='Hello {thing}', title='', subject='',
                  variables=('thing',), personal=(), version=1, registration=None):
    template = NotificationTemplate.objects.create(
        key=key, channel=channel, country=country, geo_node=geo[country], locale='en',
        category=category, body=body, title=title, subject=subject,
        variables=list(variables), personal_variables=list(personal), version=version,
        status=status, sample_values={v: f'<{v}>' for v in variables})
    if registration:
        TemplateRegistration.objects.create(template=template, **registration)
    return template


def sms_world(geo):
    """An India driver, an active SMS and in-app template for `test.event`."""
    user = make_driver_user(geo, phone='+919700000001')
    make_template(geo, channel='sms')
    make_template(geo, channel='in_app', title='Hi', body='In-app {thing}')
    return SimpleNamespace(user=user)
