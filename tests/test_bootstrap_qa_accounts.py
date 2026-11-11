"""The QA test accounts the owner signs in with by hand.

The properties that matter are about what it REFUSES to do: run in
production, invent a password, print one, or touch an account that already
exists.

THE RULING (owner, 2026-10-28)
------------------------------
    bootstrap_qa_accounts is idempotent and never changes existing accounts
    (passwords, assignments, break-glass, MFA). It may only create missing
    seed accounts.

Railway runs this on every QA boot, so "never changes" is proved the hard
way: an existing account is compared byte for byte, every column, before and
after a run that would have "fixed" it, and a second run is shown to write
no row to any table, audit log included.
"""

import io

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from servers.admin_audit.models import AdminAuditLog
from servers.driver.models import Driver
from servers.rider.models import Rider

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

ADMIN, RIDER, DRIVER = '+919850000001', '+919850000002', '+919850000003'

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


def _row(phone):
    """Every column of the account, as stored. The whole of it."""
    return User.objects.filter(phone_number=phone).values().get()


def _table_counts():
    return (User.objects.count(), Rider.objects.count(), Driver.objects.count(),
            AdminAuditLog.objects.count())


class TestItCreatesTheThreeAccounts:
    def test_all_three_exist_afterwards(self, qa_env):
        _run()

        for phone, role in [(ADMIN, 'admin'), (RIDER, 'rider'), (DRIVER, 'driver')]:
            user = User.objects.get(phone_number=phone)
            assert user.role == role
            assert user.check_password(PASSWORD)
            assert user.is_superuser is False

    def test_each_has_a_verified_email_so_it_can_actually_sign_in(self, qa_env):
        # email_login refuses an unverified address; without this the owner
        # could not sign in on the website at all.
        _run()
        for phone in [ADMIN, RIDER, DRIVER]:
            user = User.objects.get(phone_number=phone)
            assert user.email
            assert user.email_verified_at is not None

    def test_only_the_operator_is_staff(self, qa_env):
        _run()
        assert User.objects.get(phone_number=ADMIN).is_staff
        assert not User.objects.get(phone_number=RIDER).is_staff
        assert not User.objects.get(phone_number=DRIVER).is_staff

    def test_rider_and_driver_profiles_are_created(self, qa_env):
        _run()
        assert Rider.objects.filter(user_id__phone_number=RIDER).exists()
        assert Driver.objects.filter(user_id__phone_number=DRIVER).exists()

    def test_the_operator_creation_is_audited_and_the_others_are_not_access_rows(self, qa_env):
        before = AdminAuditLog.objects.count()

        _run()

        rows = list(AdminAuditLog.objects.order_by('id')[before:])
        assert rows, 'creating an operator account must leave a row'
        assert all(r.target_id == str(User.objects.get(phone_number=ADMIN).pk)
                   for r in rows)
        assert all(r.actor_label == f'{ACTOR_EMAIL} via bootstrap_qa_accounts'
                   for r in rows)


class TestItNeverExposesACredential:
    def test_the_password_is_not_printed(self, qa_env):
        output = _run()
        assert PASSWORD not in output

    def test_it_says_so_explicitly(self, qa_env):
        assert 'No password was printed' in _run()


class TestItRefusesRatherThanGuessing:
    def test_it_refuses_in_production(self, monkeypatch, qa_env):
        monkeypatch.setenv('ENVIRONMENT', 'production')
        with pytest.raises(CommandError, match='Refusing to run'):
            _run()
        assert not User.objects.filter(phone_number=ADMIN).exists()

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

        assert not User.objects.filter(phone_number=RIDER).exists()
        assert 'Skipped rider' in output
        # The others still get created.
        assert User.objects.filter(phone_number=ADMIN).exists()

    def test_a_short_password_is_refused(self, monkeypatch, qa_env):
        monkeypatch.setenv('QA_RIDER_BOOTSTRAP_PASSWORD', 'short')
        output = _run()
        assert 'Skipped rider' in output
        assert not User.objects.filter(phone_number=RIDER).exists()

    def test_it_will_not_re_role_an_existing_account(self, qa_env):
        """The rider's number already belongs to a driver: reported, and
        the account is exactly as it was."""
        User.objects.create_user(
            phone_number=RIDER, username=RIDER,
            role='driver', email='someone@saaradhigo.test')
        before = _row(RIDER)

        output = _run()

        assert _row(RIDER) == before
        assert "rider: exists, left alone; differs in: role (is 'driver', not 'rider')" in output
        assert not Rider.objects.filter(user_id__phone_number=RIDER).exists()


# ---------------------------------------------------------------------------
# An account that exists is never written, whatever it looks like
# ---------------------------------------------------------------------------

def _drifted_operator():
    """The QA operator as it might be found on a long-lived database: a
    different address, unverified, demoted, with a password of its own."""
    user = User.objects.create_user(
        phone_number=ADMIN, username=ADMIN, email='old.address@saaradhigo.test',
        role='admin', is_staff=False, password='Something-Else-Entirely-9',
    )
    assert user.email_verified_at is None
    return user


class TestAnExistingAccountIsLeftAlone:
    def test_byte_for_byte_unchanged_and_reported(self, qa_env):
        _drifted_operator()
        before = _row(ADMIN)

        output = _run()

        assert _row(ADMIN) == before, 'a column of an existing account changed'
        assert 'operator: exists, left alone; differs in: email, verified, is_staff' in output
        assert 'never changes an existing account' in output
        # Still the drifted state, still its own password.
        user = User.objects.get(phone_number=ADMIN)
        assert user.email == 'old.address@saaradhigo.test'
        assert user.email_verified_at is None
        assert user.is_staff is False
        assert user.check_password('Something-Else-Entirely-9')
        assert not user.check_password(PASSWORD)

    def test_the_other_accounts_are_still_created_around_it(self, qa_env):
        _drifted_operator()

        output = _run()

        assert 'Created: rider, driver' in output
        assert User.objects.filter(phone_number=RIDER).exists()
        assert User.objects.filter(phone_number=DRIVER).exists()

    def test_an_existing_account_gets_no_audit_row(self, qa_env):
        _drifted_operator()
        rows = AdminAuditLog.objects.count()

        _run(role='admin')

        assert AdminAuditLog.objects.count() == rows

    def test_an_existing_account_with_no_drift_is_reported_as_such(self, qa_env):
        _run()

        output = _run()

        for label in ('operator', 'rider', 'driver'):
            assert f'{label}: exists, left alone.' in output
        assert 'differs in' not in output

    def test_a_missing_profile_row_is_reported_not_created(self, qa_env):
        User.objects.create_user(
            phone_number=DRIVER, username=DRIVER, email='qa.driver@saaradhigo.test',
            role='driver', email_verified_at=timezone.now(),
        )
        before = _row(DRIVER)

        output = _run()

        assert 'driver: exists, left alone; differs in: driver profile row missing' in output
        assert _row(DRIVER) == before
        assert not Driver.objects.filter(user_id__phone_number=DRIVER).exists()

    def test_a_confirmed_factor_on_an_existing_operator_survives(self, qa_env):
        from base import ops_mfa
        user = _drifted_operator()
        device, _ = ops_mfa.enroll_totp(user)
        device.confirmed = True
        device.save(update_fields=['confirmed'])

        _run()

        assert ops_mfa.has_device(User.objects.get(phone_number=ADMIN))


class TestItIsIdempotent:
    def test_running_twice_does_not_duplicate(self, qa_env):
        _run()
        _run()
        assert User.objects.filter(
            phone_number__in=[ADMIN, RIDER, DRIVER]).count() == 3

    def test_a_second_run_writes_no_row_to_any_table(self, qa_env):
        _run()
        rows = {phone: _row(phone) for phone in (ADMIN, RIDER, DRIVER)}
        counts = _table_counts()

        _run()

        assert _table_counts() == counts
        assert {phone: _row(phone) for phone in (ADMIN, RIDER, DRIVER)} == rows

    def test_a_second_run_leaves_the_password_alone_even_if_the_variable_changed(
            self, qa_env, monkeypatch):
        _run()
        monkeypatch.setenv('QA_RIDER_BOOTSTRAP_PASSWORD', 'Totally-Different-2026!')
        _run()

        user = User.objects.get(phone_number=RIDER)
        assert user.check_password(PASSWORD), (
            'a redeploy must not silently rotate a credential in use'
        )

    def test_there_is_no_reset_password_flag(self, qa_env):
        """A flag that rewrites a credential on an existing account is the
        thing the ruling rules out. Rotate deliberately: set_operator_password
        for an operator, the website's own flow for a rider or driver."""
        _run()

        with pytest.raises(TypeError, match='reset_password'):
            _run(reset_password=True)
        assert User.objects.get(phone_number=RIDER).check_password(PASSWORD)

    def test_a_single_role_can_be_targeted(self, qa_env):
        _run(role='rider')
        assert User.objects.filter(phone_number=RIDER).exists()
        assert not User.objects.filter(phone_number=ADMIN).exists()
