"""Production must refuse to start misconfigured, and these prove it does.

The production environment is configured by copying QA's variable set. That is the
right way to avoid missing a variable, and the wrong way to handle the three QA
variables that are an authentication bypass and a back door:

    TEST_PHONE_NUMBERS            a phone that logs in with a fixed OTP, skipping
                                  SMS delivery and throttling entirely
    QA_ADMIN_BOOTSTRAP_PHONE      creates an admin account
    QA_ADMIN_BOOTSTRAP_CODE
    QA_ADMIN_BOOTSTRAP_PASSWORD

The existing ALLOWED_HOSTS guard already demonstrated its worth: it is the reason
the production container crash-loops visibly instead of serving with
ALLOWED_HOSTS=['*']. These tests extend that posture to the rest, and are written
as real boots in a subprocess rather than as assertions about settings, because
the property under test IS "the process refuses to start".

Each guard gets a negative control: the same configuration WITHOUT the fault must
boot. Otherwise a guard that rejects everything would look like a working guard.

None of these can be satisfied by setting a flag. A guard with an override is a
comment.
"""

import os
import subprocess
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# A configuration that should boot: production-shaped, nothing unsafe.
_SAFE_PRODUCTION_ENV = {
    'ENVIRONMENT': 'production',
    'DEBUG_ENV': 'False',
    'DJANGO_SECRET_KEY': 'rehearsal-only-not-a-real-key',
    'ALLOWED_HOSTS': 'api.example.in',
    'DB_HOST': 'db.internal',
    'DB_NAME': 'sg',
    'DB_USER': 'sg',
    'DB_PASSWORD': 'rehearsal-only',
    'DB_SSLMODE': 'require',
    'REDIS_URL': 'redis://redis.internal:6379',
    'CASHFREE_WEBHOOK_SECRET': 'rehearsal-only',
    'BACKEND_URL': 'https://api.example.in',
    'FRONTEND_URL': 'https://ops.example.in',
    'DJANGO_SETTINGS_MODULE': 'base.settings',
}


def _boot(**overrides):
    """Import Django settings in a fresh interpreter. Returns (rc, stderr).

    A subprocess because settings are imported once per process: a test cannot
    re-import them with different environment variables, and monkeypatching the
    module under test would prove nothing about a real boot.
    """
    env = {
        k: v for k, v in os.environ.items()
        # Drop anything from the developer's own environment that would
        # contaminate the rehearsal.
        if not k.startswith(('DJANGO_', 'DB_', 'AWS_', 'CASHFREE_', 'QA_',
                             'TEST_PHONE', 'ALLOWED_', 'REDIS_', 'DEBUG',
                             'ENVIRONMENT', 'BACKEND_URL', 'FRONTEND_URL'))
    }
    env.update(_SAFE_PRODUCTION_ENV)
    for key, value in overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value

    proc = subprocess.run(
        [sys.executable, '-c',
         'import django; django.setup(); from django.conf import settings; '
         'settings.ALLOWED_HOSTS; print("BOOT_OK")'],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=180,
    )
    return proc.returncode, (proc.stdout or '') + (proc.stderr or '')


# ---------------------------------------------------------------------------
# The negative control comes first: this configuration must boot
# ---------------------------------------------------------------------------

def test_a_correct_production_configuration_boots():
    """Without this, every test below could pass for the wrong reason.

    Also serves as the production boot rehearsal: it proves the required variable
    set is sufficient, using dummy values and touching nothing real.
    """
    rc, out = _boot()

    assert rc == 0, (
        'a correctly configured production boot failed, so the guards below '
        f'prove nothing:\n{out[-3000:]}'
    )
    assert 'BOOT_OK' in out


# ---------------------------------------------------------------------------
# Each fault must stop the boot
# ---------------------------------------------------------------------------

def test_debug_true_is_refused_in_production():
    """Django serves tracebacks with settings and locals to anyone who can
    trigger an error."""
    rc, out = _boot(DEBUG_ENV='True')

    assert rc != 0, 'production booted with DEBUG=True'
    assert 'DEBUG is True' in out


def test_a_missing_secret_key_is_refused():
    """It used to be read with a bare os.environ.get and no check, so an unset
    key surfaced later as a confusing signing error rather than at boot."""
    rc, out = _boot(DJANGO_SECRET_KEY=None)

    assert rc != 0, 'production booted with no DJANGO_SECRET_KEY'
    assert 'DJANGO_SECRET_KEY' in out


def test_missing_allowed_hosts_is_still_refused():
    """The guard that is currently keeping production honest. Do not lose it."""
    rc, out = _boot(ALLOWED_HOSTS=None)

    assert rc != 0, 'production booted with no ALLOWED_HOSTS'
    assert 'ALLOWED_HOSTS' in out


def test_the_otp_bypass_is_refused_in_production():
    """The single most dangerous variable to copy from QA.

    Each entry is a phone number that authenticates with a fixed OTP and skips
    both SMS delivery and throttling.
    """
    rc, out = _boot(TEST_PHONE_NUMBERS='+919999000001:100001')

    assert rc != 0, 'production booted with an OTP bypass configured'
    assert 'TEST_PHONE_NUMBERS' in out
    assert 'authentication bypass' in out


@pytest.mark.parametrize('qa_var', [
    'QA_ADMIN_BOOTSTRAP_PHONE',
    'QA_ADMIN_BOOTSTRAP_CODE',
    'QA_ADMIN_BOOTSTRAP_PASSWORD',
])
def test_the_qa_admin_bootstrap_is_refused_in_production(qa_var):
    """It exists to create an admin account in QA. In production that is a back
    door into payout approval and KYC."""
    rc, out = _boot(**{qa_var: 'something'})

    assert rc != 0, f'production booted with {qa_var} set'
    assert qa_var in out
    assert 'back door' in out


@pytest.mark.parametrize('url_var,bad_value', [
    ('BACKEND_URL', 'https://backend-qa-811d.up.railway.app'),
    ('FRONTEND_URL', 'https://ops-staging.example.in'),
    ('BACKEND_URL', 'http://localhost:8000'),
])
def test_pointing_production_at_qa_infrastructure_is_refused(url_var, bad_value):
    """Receipts and notifications would carry links into a test system, and real
    customer data would be written there."""
    rc, out = _boot(**{url_var: bad_value})

    assert rc != 0, f'production booted with {url_var}={bad_value}'
    assert url_var in out


# ---------------------------------------------------------------------------
# The guards must NOT fire outside production
# ---------------------------------------------------------------------------

def test_qa_may_keep_its_test_phones():
    """QA also runs DEBUG=False and legitimately needs the bypass.

    This is why the guards key on ENVIRONMENT rather than `not DEBUG`. Getting
    this wrong would break QA's login the moment the guards shipped.
    """
    rc, out = _boot(
        ENVIRONMENT='qa',
        TEST_PHONE_NUMBERS='+919999000001:100001',
        BACKEND_URL='https://backend-qa-811d.up.railway.app',
        QA_ADMIN_BOOTSTRAP_PHONE='+919999000009',
    )

    assert rc == 0, f'a legitimate QA configuration was refused:\n{out[-3000:]}'
    assert 'BOOT_OK' in out


def test_development_is_unaffected():
    """A developer running DEBUG=True must not meet any of this."""
    rc, out = _boot(
        ENVIRONMENT=None, DEBUG_ENV='True', ALLOWED_HOSTS=None,
        TEST_PHONE_NUMBERS='+919999000001:100001',
    )

    assert rc == 0, f'a local development boot was refused:\n{out[-3000:]}'


def test_an_absent_environment_with_debug_off_is_treated_as_production():
    """The conservative reading.

    A deployment that forgot ENVIRONMENT should get the strict checks, not the
    permissive ones.
    """
    rc, out = _boot(ENVIRONMENT=None, TEST_PHONE_NUMBERS='+919999000001:100001')

    assert rc != 0, (
        'a DEBUG=False boot with no ENVIRONMENT accepted an OTP bypass; an '
        'unlabelled deployment must be treated as production'
    )


def test_all_faults_are_reported_together():
    """An operator should get the whole list, not one fault per attempt.

    Fixing these one boot at a time across a real deployment is how a launch
    window gets consumed.
    """
    rc, out = _boot(
        DJANGO_SECRET_KEY=None,
        TEST_PHONE_NUMBERS='+919999000001:100001',
        QA_ADMIN_BOOTSTRAP_PASSWORD='x',
    )

    assert rc != 0
    assert 'DJANGO_SECRET_KEY' in out
    assert 'TEST_PHONE_NUMBERS' in out
    assert 'QA_ADMIN_BOOTSTRAP_PASSWORD' in out
