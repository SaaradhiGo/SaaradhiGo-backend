"""The QA operator bootstrap must work in QA and refuse everywhere else.

Three variables -- QA_ADMIN_BOOTSTRAP_PHONE, QA_ADMIN_BOOTSTRAP_CODE,
QA_ADMIN_BOOTSTRAP_PASSWORD -- sat in the QA environment for months with nothing in
the codebase reading any of them. They looked like a working admin bootstrap, which
is worse than having none: an entire run could not rehearse a single operator
workflow, and the reason was that the capability was imaginary rather than missing.

These tests hold the properties that make making them real safe:

  * it refuses to run in production, and treats anything that cannot prove it is
    not production as production;
  * there is no default password and no default phone;
  * it never emits the credential;
  * it is idempotent, and will not rotate a password nobody asked it to rotate;
  * it creates an operator, not a rider with a flag set.
"""

from io import StringIO

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError

from servers.admin_audit.models import AdminAuditLog

User = get_user_model()

pytestmark = pytest.mark.django_db

PHONE = '+919555000001'
GOOD_PASSWORD = 'qa-operator-not-a-real-secret'
ACTOR_EMAIL = 'ops.lead@saaradhigo.test'
ACTOR_PHONE = '+919555000099'


@pytest.fixture
def qa_env(monkeypatch, db):
    # The operator the run is attributed to. Without one in the database
    # the command would take the genesis path instead, which is tested in
    # test_actor_email_mandatory.py; here every run has a name.
    User.objects.create_user(
        phone_number=ACTOR_PHONE, username=ACTOR_PHONE, email=ACTOR_EMAIL,
        full_name='Ops Lead', role='admin', is_staff=True,
    )
    monkeypatch.setenv('ENVIRONMENT', 'qa')
    monkeypatch.setenv('DEBUG_ENV', 'False')
    monkeypatch.setenv('QA_ADMIN_BOOTSTRAP_PHONE', PHONE)
    monkeypatch.setenv('QA_ADMIN_BOOTSTRAP_PASSWORD', GOOD_PASSWORD)
    monkeypatch.delenv('QA_ADMIN_BOOTSTRAP_CODE', raising=False)


def _run(**kwargs):
    out, err = StringIO(), StringIO()
    kwargs.setdefault('actor_email', ACTOR_EMAIL)
    call_command('bootstrap_qa_operator', stdout=out, stderr=err, **kwargs)
    return out.getvalue() + err.getvalue()


# ---------------------------------------------------------------------------
# It refuses outside QA
# ---------------------------------------------------------------------------

def test_it_refuses_in_production(monkeypatch, qa_env):
    monkeypatch.setenv('ENVIRONMENT', 'production')

    with pytest.raises(CommandError) as exc:
        _run()

    assert 'production' in str(exc.value).lower()
    assert not User.objects.filter(phone_number=PHONE).exists()


def test_an_unlabelled_non_debug_deployment_is_treated_as_production(
    monkeypatch, qa_env,
):
    """Anything that cannot prove it is not production is production."""
    monkeypatch.delenv('ENVIRONMENT', raising=False)
    monkeypatch.setenv('DEBUG_ENV', 'False')

    with pytest.raises(CommandError):
        _run()

    assert not User.objects.filter(phone_number=PHONE).exists()


def test_local_development_is_allowed(monkeypatch, qa_env):
    """A developer running DEBUG=True must be able to make themselves an operator."""
    monkeypatch.delenv('ENVIRONMENT', raising=False)
    monkeypatch.setenv('DEBUG_ENV', 'True')

    _run()

    assert User.objects.filter(phone_number=PHONE, role='admin').exists()


# ---------------------------------------------------------------------------
# No defaults, ever
# ---------------------------------------------------------------------------

def test_a_missing_phone_is_refused(monkeypatch, qa_env):
    monkeypatch.delenv('QA_ADMIN_BOOTSTRAP_PHONE', raising=False)

    with pytest.raises(CommandError) as exc:
        _run()

    assert 'PHONE' in str(exc.value)


def test_a_missing_password_is_refused(monkeypatch, qa_env):
    """A built-in admin password is how a test account becomes an incident."""
    monkeypatch.delenv('QA_ADMIN_BOOTSTRAP_PASSWORD', raising=False)

    with pytest.raises(CommandError) as exc:
        _run()

    assert 'PASSWORD' in str(exc.value)
    assert not User.objects.filter(phone_number=PHONE).exists()


def test_a_short_password_is_refused(monkeypatch, qa_env):
    monkeypatch.setenv('QA_ADMIN_BOOTSTRAP_PASSWORD', 'short')

    with pytest.raises(CommandError) as exc:
        _run()

    assert '12' in str(exc.value)


# ---------------------------------------------------------------------------
# What it creates
# ---------------------------------------------------------------------------

def test_it_creates_an_operator_not_a_rider(qa_env):
    _run()

    user = User.objects.get(phone_number=PHONE)
    assert user.role == 'admin'
    assert user.is_staff is True
    assert user.is_superuser is True


def test_the_created_operator_can_actually_sign_in(qa_env, client, frozen_console_open):
    """The point of the whole exercise.

    An account that exists but cannot reach the console would leave every
    operator workflow exactly as unrehearsable as before.
    """
    _run()

    user = User.objects.get(phone_number=PHONE)
    assert user.check_password(GOOD_PASSWORD)

    resp = client.post('/login/', {'username': PHONE, 'password': GOOD_PASSWORD})
    assert resp.status_code in (301, 302), (
        f'operator login returned {resp.status_code} rather than a redirect'
    )
    # And the session reaches an operator-only page. The bootstrap account is
    # a superuser, which the frozen console counts as Super Admin; the route
    # is opened by `frozen_console_open` because the shipped allowlist is
    # the admin index alone.
    assert client.get('/stale-rides/').status_code == 200


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

def test_running_twice_creates_one_account(qa_env):
    _run()
    _run()

    assert User.objects.filter(phone_number=PHONE).count() == 1


def test_a_rerun_does_not_rotate_the_password(qa_env, monkeypatch):
    """A redeploy must not silently change a credential an operator is using."""
    _run()
    monkeypatch.setenv('QA_ADMIN_BOOTSTRAP_PASSWORD', 'a-completely-different-one')

    _run()

    user = User.objects.get(phone_number=PHONE)
    assert user.check_password(GOOD_PASSWORD), (
        'the password was rotated without --reset-password'
    )


def test_there_is_no_reset_password_flag(qa_env):
    """A flag that rewrites a credential on an existing account is exactly what
    the create-only ruling (AUDIT 27.40) removes; `set_operator_password` is the
    tool for that, with its own actor and reason."""
    from django.core.management import get_commands, load_command_class

    command = load_command_class(get_commands()['bootstrap_qa_operator'], 'bootstrap_qa_operator')
    parser = command.create_parser('manage.py', 'bootstrap_qa_operator')
    assert '--reset-password' not in parser.format_help()


def test_a_drifted_account_is_reported_and_left_alone(qa_env):
    """An account that lost its operator role is NOT repaired by a bootstrap."""
    User.objects.create_user(phone_number=PHONE, role='rider')

    output = _run()

    user = User.objects.get(phone_number=PHONE)
    assert user.role == 'rider'
    assert not user.is_staff and not user.is_superuser
    assert not user.has_usable_password()
    assert 'left alone' in output and 'role' in output
    assert User.objects.filter(phone_number=PHONE).count() == 1


# ---------------------------------------------------------------------------
# It must not leak the credential
# ---------------------------------------------------------------------------

def test_the_password_never_appears_in_the_output(qa_env):
    output = _run()

    assert GOOD_PASSWORD not in output
    assert 'password' not in output or 'left unchanged' in output or \
        'fields set' in output


def test_the_password_never_appears_in_the_audit_row(qa_env):
    _run()

    row = AdminAuditLog.objects.filter(action='qa_operator_bootstrapped').first()
    assert row is not None, 'the bootstrap was not audited'
    serialised = str(row.before) + str(row.after) + row.reason + row.actor_label
    assert GOOD_PASSWORD not in serialised
    assert PHONE not in serialised, (
        'the audit row carries the phone number; the user id is enough to find '
        'the account and an audit log is retained and searchable'
    )


def test_the_bootstrap_is_audited(qa_env):
    _run()

    row = AdminAuditLog.objects.filter(action='qa_operator_bootstrapped').first()
    assert row is not None
    assert row.target_type == 'User'
    assert row.after.get('created') is True
    assert row.after.get('environment') == 'qa'


def test_the_ignored_code_variable_is_not_treated_as_a_second_factor(
    qa_env, monkeypatch,
):
    """QA_ADMIN_BOOTSTRAP_CODE is accepted and ignored.

    It exists only so the existing variable set does not need editing. Nothing
    should read it as an MFA secret, because it is not one.
    """
    monkeypatch.setenv('QA_ADMIN_BOOTSTRAP_CODE', 'irrelevant')

    output = _run()

    assert 'irrelevant' not in output
    user = User.objects.get(phone_number=PHONE)
    assert user.check_password(GOOD_PASSWORD)
