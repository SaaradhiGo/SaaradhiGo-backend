"""Settings used by the test suite. Thin wrapper around `base.settings`.

`base.settings` deliberately refuses to boot with a production-shaped gap:
`ALLOWED_HOSTS` and `DB_HOST` are required when `DEBUG=False`, and
`servers.payments.apps` refuses to start without `CASHFREE_WEBHOOK_SECRET`.
Those guards are correct and must not be softened.

They also meant the suite only ran if the caller had already exported six
environment variables, which is how the documented command drifted apart
from what CI actually ran. This module supplies exactly those values and
then imports the real settings, so `base.settings` remains the module under
test — nothing here overrides an application setting.

A root `conftest.py` cannot do this: pytest-django reads `DATABASES` inside
`pytest_load_initial_conftests`, which is too early for a conftest to have
run. Hence a settings module.

Every assignment uses `setdefault`, so a value already exported — by CI, by
a developer, by a container — wins untouched.

None of these are secrets. `DEBUG_ENV=True` keeps the boot-time guards
permissive and selects the SQLite fallback, so a stray `DB_HOST` in a
developer's shell cannot point the suite at a real database. The values
mirror the `Run pytest` step in .github/workflows/deploy.yml.

Not named `*_test.py` / `test_*.py` on purpose — either would be collected
as a test module by the `python_files` patterns in pytest.ini.
"""

import os

_TEST_ENV_DEFAULTS = {
    'DJANGO_SECRET_KEY': 'ci-test-secret-not-for-prod',
    'ALLOWED_HOSTS': 'localhost,127.0.0.1,testserver',
    'DEBUG_ENV': 'True',
    'CASHFREE_WEBHOOK_SECRET': 'ci-test-secret',
    'REDIS_URL': 'redis://localhost:6379',

    # A SENDER, so the mail paths are exercised rather than skipped.
    #
    # base/auth_email.py refuses to report a send as successful when the
    # deployment has no From address -- deliberately, because that is the live
    # state of QA and it is why receipt email has been failing into a database
    # column instead of a log. Without this, every test that asserts a message
    # was composed would fail for the right reason in the wrong place.
    #
    # Nothing leaves the machine: Django swaps EMAIL_BACKEND for locmem under
    # test, which captures into mail.outbox. QA and production must still set
    # a real DEFAULT_FROM_EMAIL on a verified SES sender domain -- an owner
    # action, documented in the SES setup runbook.
    'DEFAULT_FROM_EMAIL': 'no-reply@ci.saaradhigo.test',

    # OTP gating ON, as every non-production environment must have it. The
    # seed gate (base/seeding.py) refuses to load test accounts without it,
    # because the seeded numbers are a real-looking range. test_seed_guard.py
    # empties this deliberately to prove the refusal.
    'TEST_PHONE_NUMBERS': '+919999000001:100001,+919999000002:100002,+919999000009:100009',
}

for _key, _value in _TEST_ENV_DEFAULTS.items():
    os.environ.setdefault(_key, _value)

# Import the real settings *after* the environment is populated, so every
# guard in base.settings evaluates against it. This is the module under test.
from base.settings import *  # noqa: F401,F403,E402
