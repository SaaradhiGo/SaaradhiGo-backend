"""The QA test accounts the owner signs in with by hand.

The properties that matter are about what it REFUSES to do: run in
production, invent a password, print one, or quietly re-role an account that
already exists.
"""

import io

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError

pytestmark = pytest.mark.django_db

User = get_user_model()

PASSWORD = 'Peacock-Junction-2026!'

ENV = {
    'ENVIRONMENT': 'qa',
    'DEBUG_ENV': 'True',
    'QA_ADMIN_BOOTSTRAP_PHONE': '+919850000001',
    'QA_ADMIN_BOOTSTRAP_EMAIL': 'qa.admin@saaradhigo.test',
    'QA_ADMIN_BOOTSTRAP_PASSWORD': PASSWORD,
    'QA_RIDER_BOOTSTRAP_PHONE': '+919850000002',
    'QA_RIDER_BOOTSTRAP_EMAIL': 'qa.rider@saaradhigo.test',
    'QA_RIDER_BOOTSTRAP_PASSWORD': PASSWORD,
    'QA_DRIVER_BOOTSTRAP_PHONE': '+919850000003',
    'QA_DRIVER_BOOTSTRAP_EMAIL': 'qa.driver@saaradhigo.test',
    'QA_DRIVER_BOOTSTRAP_PASSWORD': PASSWORD,
}


ACTOR_EMAIL = 'ops.lead@saaradhigo.test'
ACTOR_PHONE = '+919850000099'


@pytest.fixture
def qa_env(monkeypatch, settings, db):
    # The operator the run is attributed to; see test_actor_email_mandatory
    # for the empty-database (genesis) path.
    User.objects.create_user(
        phone_number=ACTOR_PHONE, username=ACTOR_PHONE, email=ACTOR_EMAIL,
        full_name='Ops Lead', role='admin', is_staff=True,
    )
    # The seed gate (base/seeding.py) refuses to load test accounts where OTP
    # gating is off, because the seeded numbers are a real-looking range. QA
    # sets TEST_PHONE_NUMBERS; so must the fixture that stands in for QA.
    settings.TEST_PHONE_NUMBERS = {
        '+919999000001': '100001', '+919999000002': '100002',
        '+919999000009': '100009',
    }
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    return ENV


def _run(**kwargs):
    out = io.StringIO()
    kwargs.setdefault('actor_email', ACTOR_EMAIL)
    call_command('bootstrap_qa_accounts', stdout=out, stderr=out, **kwargs)
    return out.getvalue()


class TestItCreatesTheThreeAccounts:
    def test_all_three_exist_afterwards(self, qa_env):
        _run()

        for phone, role in [('+919850000001', 'admin'),
                            ('+919850000002', 'rider'),
                            ('+919850000003', 'driver')]:
            user = User.objects.get(phone_number=phone)
            assert user.role == role
            assert user.check_password(PASSWORD)

    def test_each_has_a_verified_email_so_it_can_actually_sign_in(self, qa_env):
        # email_login refuses an unverified address; without this the owner
        # could not sign in on the website at all.
        _run()
        for phone in ['+919850000001', '+919850000002', '+919850000003']:
            user = User.objects.get(phone_number=phone)
            assert user.email
            assert user.email_verified_at is not None

    def test_only_the_operator_is_staff(self, qa_env):
        _run()
        assert User.objects.get(phone_number='+919850000001').is_staff
        assert not User.objects.get(phone_number='+919850000002').is_staff
        assert not User.objects.get(phone_number='+919850000003').is_staff

    def test_rider_and_driver_profiles_are_created(self, qa_env):
        from servers.driver.models import Driver
        from servers.rider.models import Rider
        _run()
        assert Rider.objects.filter(
            user_id__phone_number='+919850000002').exists()
        assert Driver.objects.filter(
            user_id__phone_number='+919850000003').exists()


class TestItNeverExposesACredential:
    def test_the_password_is_not_printed(self, qa_env):
        output = _run()
        assert PASSWORD not in output
        assert 'password' not in output.lower().split('password unchanged')[0] \
            or PASSWORD not in output

    def test_it_says_so_explicitly(self, qa_env):
        assert 'No password was printed' in _run()


class TestItRefusesRatherThanGuessing:
    def test_it_refuses_in_production(self, monkeypatch, qa_env):
        monkeypatch.setenv('ENVIRONMENT', 'production')
        with pytest.raises(CommandError, match='Refusing to run'):
            _run()
        assert not User.objects.filter(phone_number='+919850000001').exists()

    def test_an_unlabelled_non_debug_deployment_is_treated_as_production(
            self, monkeypatch, qa_env):
        monkeypatch.setenv('ENVIRONMENT', '')
        monkeypatch.setenv('DEBUG_ENV', 'False')
        with pytest.raises(CommandError, match='Refusing to run'):
            _run()

    def test_a_missing_password_skips_that_role_rather_than_inventing_one(
            self, monkeypatch, qa_env):
        monkeypatch.delenv('QA_RIDER_BOOTSTRAP_PASSWORD')
        output = _run()

        assert not User.objects.filter(phone_number='+919850000002').exists()
        assert 'Skipped rider' in output
        # The others still get created.
        assert User.objects.filter(phone_number='+919850000001').exists()

    def test_a_short_password_is_refused(self, monkeypatch, qa_env):
        monkeypatch.setenv('QA_RIDER_BOOTSTRAP_PASSWORD', 'short')
        output = _run()
        assert 'Skipped rider' in output
        assert not User.objects.filter(phone_number='+919850000002').exists()

    def test_it_will_not_re_role_an_existing_account(self, qa_env):
        User.objects.create_user(
            phone_number='+919850000002', username='+919850000002',
            role='driver', email='someone@saaradhigo.test')

        with pytest.raises(CommandError, match='Refusing to change it'):
            _run()


class TestItIsIdempotent:
    def test_running_twice_does_not_duplicate(self, qa_env):
        _run()
        _run()
        assert User.objects.filter(
            phone_number__in=['+919850000001', '+919850000002',
                              '+919850000003']).count() == 3

    def test_a_second_run_leaves_the_password_alone(self, qa_env, monkeypatch):
        _run()
        monkeypatch.setenv('QA_RIDER_BOOTSTRAP_PASSWORD', 'Totally-Different-2026!')
        _run()

        user = User.objects.get(phone_number='+919850000002')
        assert user.check_password(PASSWORD), (
            'a redeploy must not silently rotate a credential in use'
        )

    def test_reset_password_rotates_it_deliberately(self, qa_env, monkeypatch):
        _run()
        monkeypatch.setenv('QA_RIDER_BOOTSTRAP_PASSWORD', 'Totally-Different-2026!')
        _run(reset_password=True)

        assert User.objects.get(
            phone_number='+919850000002').check_password('Totally-Different-2026!')

    def test_a_single_role_can_be_targeted(self, qa_env):
        _run(role='rider')
        assert User.objects.filter(phone_number='+919850000002').exists()
        assert not User.objects.filter(phone_number='+919850000001').exists()
