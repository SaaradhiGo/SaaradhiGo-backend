"""The Celery worker must not consume tasks against an unmigrated database.

The backend runs `migrate` as its pre-deploy step; the worker does not, and
nothing orders one after the other. base/migration_gate.py holds the worker at
`worker_init` until the migration plan is empty. These tests pin the four
behaviours that make that a gate rather than a delay:

  * it returns at once when there is nothing to apply, and sleeps for nothing;
  * it retries with growing, capped delays while a migration is pending or the
    database is unreachable, and lets the process through the moment the plan
    empties;
  * on timeout it names the pending migration at CRITICAL and raises
    SystemExit(1) -- which Celery's signal dispatcher does not swallow -- so the
    worker exits instead of consuming;
  * MIGRATION_WAIT_TIMEOUT=0 skips it without touching the database.

Plus the two wirings: the `worker_init` receiver is connected and calls the
gate, and `manage.py wait_for_migrations` exits 0 / 1 for the same reasons.

Time is faked throughout. `time.sleep` advances a fake `time.monotonic`, so the
gate experiences real elapsed time without the suite paying for it, and the
sequence of sleeps is observable.
"""

import logging
from unittest import mock

import pytest
from django.core.management import call_command
from django.db import OperationalError
from django.db.migrations import Migration
from django.db.migrations.executor import MigrationExecutor

from base import migration_gate
from base.migration_gate import GateResult, wait_for_migrations

pytestmark = pytest.mark.django_db

PENDING = [(Migration('0042_add_settlement_batch', 'payments'), False)]


class FakeClock:
    """`time.sleep` that moves `time.monotonic` forward, and records itself."""

    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    # Replaces the `time` module AS SEEN BY the gate, not the global one, so
    # nothing else in the process (Django's connection ageing, pytest's own
    # timing) sees a clock that jumps.
    fake = FakeClock()
    monkeypatch.setattr(migration_gate, 'time', fake)
    return fake


@pytest.fixture
def gate_log(caplog):
    caplog.set_level(logging.INFO, logger='base.migration_gate')
    return caplog


def plan_returning(*plans):
    """Patch `MigrationExecutor.migration_plan` to return each plan in turn."""
    return mock.patch.object(MigrationExecutor, 'migration_plan', side_effect=list(plans))


# ---------------------------------------------------------------------------
# Migrated already: through at once
# ---------------------------------------------------------------------------

def test_a_migrated_database_passes_immediately_without_sleeping(clock, gate_log):
    # The test database is fully migrated by pytest-django, so this is the real
    # executor against the real schema: no mocking of the plan.
    result = wait_for_migrations(timeout=30)

    assert result == GateResult(attempts=1, waited_seconds=0.0, skipped=False)
    assert clock.sleeps == []
    passed = [r for r in gate_log.records if r.levelno == logging.INFO]
    assert len(passed) == 1
    assert 'Migration gate passed' in passed[0].getMessage()
    assert '1 attempt(s)' in passed[0].getMessage()
    assert 'waited 0.0s' in passed[0].getMessage()


# ---------------------------------------------------------------------------
# Pending forever: backoff, then a loud exit
# ---------------------------------------------------------------------------

def test_a_pending_migration_is_retried_with_backoff_then_exits_1(clock, gate_log):
    with plan_returning(*([PENDING] * 20)):
        with pytest.raises(SystemExit) as raised:
            wait_for_migrations(timeout=30, initial_delay=1, max_delay=15)

    assert raised.value.code == 1
    # 1, 2, 4, 8 double; 15 is the cap; the sum is exactly the 30 s budget, so
    # the final check happens at the deadline and nothing sleeps past it.
    assert clock.sleeps == [1, 2, 4, 8, 15]

    critical = [r for r in gate_log.records if r.levelno == logging.CRITICAL]
    assert len(critical) == 1
    message = critical[0].getMessage()
    assert 'TIMED OUT' in message
    assert 'payments.0042_add_settlement_batch' in message, 'name what to apply'
    assert 'MIGRATION_WAIT_TIMEOUT=0' in message, 'name the off switch'


def test_the_last_sleep_is_clamped_to_the_deadline(clock):
    with plan_returning(*([PENDING] * 20)):
        with pytest.raises(SystemExit):
            wait_for_migrations(timeout=20, initial_delay=1, max_delay=15)

    # 1 + 2 + 4 + 8 = 15; the next would be 15 but only 5 s of budget remain.
    assert clock.sleeps == [1, 2, 4, 8, 5]


def test_the_warning_while_waiting_says_what_is_pending(clock, gate_log):
    with plan_returning(PENDING, []):
        wait_for_migrations(timeout=30)

    waiting = [r for r in gate_log.records if r.levelno == logging.WARNING]
    assert len(waiting) == 1
    assert '1 pending migration(s)' in waiting[0].getMessage()
    assert 'retrying in 1.0s' in waiting[0].getMessage()


# ---------------------------------------------------------------------------
# Becomes ready: through as soon as the plan empties
# ---------------------------------------------------------------------------

def test_passes_on_the_third_attempt_after_two_sleeps(clock, gate_log):
    with plan_returning(PENDING, PENDING, []) as plan:
        result = wait_for_migrations(timeout=300, initial_delay=1, max_delay=15)

    assert plan.call_count == 3
    assert clock.sleeps == [1, 2]
    assert result.attempts == 3
    assert result.waited_seconds == 3.0
    assert result.skipped is False
    passed = [r for r in gate_log.records if r.levelno == logging.INFO]
    assert '3 attempt(s), waited 3.0s' in passed[-1].getMessage()


def test_an_unreachable_database_is_not_ready_rather_than_an_error(clock, gate_log):
    ready = mock.Mock()
    ready.migration_plan.return_value = []
    with mock.patch(
        'django.db.migrations.executor.MigrationExecutor',
        side_effect=[OperationalError('connection refused'), ready],
    ):
        result = wait_for_migrations(timeout=300)

    assert result.attempts == 2
    assert clock.sleeps == [1]
    waiting = [r for r in gate_log.records if r.levelno == logging.WARNING]
    assert 'database not reachable (connection refused)' in waiting[0].getMessage()


def test_a_database_that_never_answers_times_out_loudly(clock, gate_log):
    with mock.patch(
        'django.db.migrations.executor.MigrationExecutor',
        side_effect=OperationalError('connection refused'),
    ):
        with pytest.raises(SystemExit) as raised:
            wait_for_migrations(timeout=5, initial_delay=1, max_delay=15)

    assert raised.value.code == 1
    critical = [r for r in gate_log.records if r.levelno == logging.CRITICAL]
    assert 'database never became reachable' in critical[0].getMessage()


# ---------------------------------------------------------------------------
# Disabled: skipped, database untouched
# ---------------------------------------------------------------------------

def test_timeout_zero_skips_the_gate_without_touching_the_database(clock, gate_log):
    with mock.patch('django.db.migrations.executor.MigrationExecutor') as executor:
        result = wait_for_migrations(timeout=0)

    assert result == GateResult(attempts=0, waited_seconds=0.0, skipped=True)
    executor.assert_not_called()
    assert clock.sleeps == []
    warned = [r for r in gate_log.records if r.levelno == logging.WARNING]
    assert len(warned) == 1
    assert 'DISABLED' in warned[0].getMessage()


def test_the_timeout_defaults_to_the_setting(clock, settings):
    settings.MIGRATION_WAIT_TIMEOUT = 0
    with mock.patch('django.db.migrations.executor.MigrationExecutor') as executor:
        assert wait_for_migrations().skipped is True
    executor.assert_not_called()

    settings.MIGRATION_WAIT_TIMEOUT = 3
    with plan_returning(*([PENDING] * 10)):
        with pytest.raises(SystemExit):
            wait_for_migrations(initial_delay=1, max_delay=15)
    assert sum(clock.sleeps) == 3


def test_the_default_timeout_is_300_seconds(settings):
    # Set from MIGRATION_WAIT_TIMEOUT with the same one-line idiom as every
    # other environment-backed setting; what matters is the default it lands
    # on when the variable is unset, which is what QA and production run with.
    assert settings.MIGRATION_WAIT_TIMEOUT == 300


# ---------------------------------------------------------------------------
# Wiring: the Celery worker and the management command
# ---------------------------------------------------------------------------

def test_the_worker_init_signal_is_connected_and_calls_the_gate():
    # `worker_init.send()` is deliberately NOT used here: the Django fixup is
    # also connected to it and would install its task-lifecycle hooks into this
    # test process for the rest of the session. The receiver is called the way
    # the dispatcher calls it instead.
    from celery.signals import worker_init

    import base.celery  # noqa: F401 - connecting happens at import

    receivers = worker_init._live_receivers(sender=None)
    assert migration_gate.hold_worker_until_migrated in receivers, (
        'the gate must be connected to worker_init, the last hook in the main '
        'worker process before the pool, embedded beat and consumer start'
    )

    with mock.patch.object(migration_gate, 'wait_for_migrations') as gate:
        migration_gate.hold_worker_until_migrated(
            sender=mock.Mock(name='WorkController'), signal=worker_init,
        )
    gate.assert_called_once_with()


def test_a_timeout_in_the_signal_handler_is_a_systemexit_celery_cannot_swallow(clock, settings):
    # Celery's dispatcher catches Exception from receivers and logs it. The gate
    # raises SystemExit, which is a BaseException, so it reaches the worker. A
    # private Signal proves the dispatcher property without firing the real one.
    # The receiver takes no arguments, so this exercises the configured timeout.
    from celery.utils.dispatch import Signal

    settings.MIGRATION_WAIT_TIMEOUT = 5
    probe = Signal(name='probe_worker_init')
    probe.connect(migration_gate.hold_worker_until_migrated, weak=False)

    with mock.patch.object(MigrationExecutor, 'migration_plan', return_value=PENDING):
        with pytest.raises(SystemExit) as raised:
            probe.send(sender=mock.Mock(name='WorkController'))
    assert raised.value.code == 1
    assert clock.sleeps == [1, 2, 2]


def test_the_management_command_exits_0_when_migrated(clock):
    from io import StringIO

    out = StringIO()
    call_command('wait_for_migrations', stdout=out)

    assert 'Database is fully migrated' in out.getvalue()
    assert clock.sleeps == []


def test_the_management_command_exits_1_on_timeout(clock, gate_log):
    with plan_returning(*([PENDING] * 10)):
        with pytest.raises(SystemExit) as raised:
            call_command('wait_for_migrations', timeout=3)

    assert raised.value.code == 1
    assert clock.sleeps == [1, 2]
    critical = [r for r in gate_log.records if r.levelno == logging.CRITICAL]
    assert 'payments.0042_add_settlement_batch' in critical[0].getMessage()


def test_the_management_command_reports_a_skipped_gate():
    from io import StringIO

    out = StringIO()
    with mock.patch('django.db.migrations.executor.MigrationExecutor') as executor:
        call_command('wait_for_migrations', timeout=0, stdout=out)

    executor.assert_not_called()
    assert 'skipped' in out.getvalue()
