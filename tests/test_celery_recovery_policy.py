"""§9 — a recovery objective per task, not one broker setting for everything.

WHY THIS FILE IS A TEST AND NOT A DOCUMENT
------------------------------------------
§9 asks for each critical task to be classified by acceptable recovery objective, and
for a decision per task: task-specific recovery, queue separation, or existing
behaviour. A table in a runbook drifts the moment somebody adds a task. A table that
fails the suite does not.

So the classification lives here, and each row is checked against the thing that
actually implements it.

THE MEASUREMENT EVERYTHING BELOW DEPENDS ON
-------------------------------------------
From `qa/celery_worker_loss_drill.py`, on Linux with a real SIGKILL: a task whose
worker dies is redelivered after `visibility_timeout` plus up to ~100 s of
restore-poll granularity. At the configured 900 s that is roughly 15-17 minutes.

That number is the DEFAULT recovery objective for every task. The question §9 asks is
which tasks cannot live with it.

WHY visibility_timeout WAS NOT SIMPLY LOWERED
---------------------------------------------
Its floor is the longest a message can legitimately sit unacked: a 180 s countdown
(TRIP_ACTUALS_DELAY_SECONDS, during which an ETA message waits in worker memory)
plus a 360 s execution (CELERY_TASK_TIME_LIMIT) = 540 s. Below that, a second worker
takes a message the first is still working on, and recovery becomes a
duplicate-execution generator.

One setting cannot serve a 90-second deadline and a six-minute task. Hence per-task
answers.

THE CLASSIFICATION

  task                     objective     decision
  ------------------------ ------------- --------------------------------------------
  auto_cancel_trip         ~2 min        TASK-SPECIFIC RECOVERY. A rider is watching
                                         a search; 15 min of silence is the failure.
                                         ride.sweep_unaccepted_trips finds overdue
                                         `requested` trips from the DATABASE every
                                         2 min, so recovery no longer depends on the
                                         broker at all.
  dispatch_wave            ~2 min        COVERED BY THE ABOVE. If a wave is lost the
                                         rider's wait is still bounded by the accept
                                         deadline, which now has a durable path.
  send_otp_via_sns         seconds       ALTERNATE PATH, user-driven. A rider who
                                         gets no SMS requests another OTP; that is
                                         faster than any redelivery and it is the
                                         behaviour they already expect. What matters
                                         is that a failure is not reported as
                                         success -- covered separately.
  send_push_notification   seconds       EXISTING BEHAVIOUR. Push is the fallback
                                         channel; the WebSocket is primary. A late
                                         push is not worth its own machinery.
  dispatch_sos             seconds       ALTERNATE PATH, operator-driven. The
                                         SOSEvent row is the durable record and the
                                         operator queue is polled, so an SOS whose
                                         notification was lost is still VISIBLE. The
                                         task also deliberately re-alerts on
                                         redelivery.
  issue_receipt_for_trip   tolerant      EXISTING BEHAVIOUR, plus a resend endpoint.
                                         Nobody is harmed by a receipt arriving
                                         fifteen minutes late.
  persist_location_trail   tolerant      EXISTING BEHAVIOUR. A periodic drain: the
                                         next tick picks up whatever the lost run
                                         did not, and (trip, source_event_id) makes
                                         re-processing a no-op.
  compute_trip_actuals     tolerant      EXISTING BEHAVIOUR. Observe-only, writes no
                                         final_fare, so lateness costs nothing.
  the reconcile sweeps     tolerant      EXISTING BEHAVIOUR. They ARE the recovery
                                         mechanism for something else, and they run
                                         on their own schedule.

QUEUE SEPARATION WAS CONSIDERED AND NOT DONE
--------------------------------------------
Routing the rider-facing tasks to their own queue with their own worker would give
them independent redelivery. It is the right answer at a scale where one slow task
class can starve another. At pilot scale, with a four-child worker and a measured
Celery backlog of 0 through 100 concurrent rides, it would add a second worker
deployment, a second thing to monitor and a new way to misconfigure routing, to solve
a queueing problem that has not been observed. Recorded as the next step if backlog
ever becomes non-zero, rather than built speculatively.
"""

import pytest
from django.conf import settings

# The recovery objective in seconds that broker redelivery alone provides, from the
# measured drill. Any task that cannot live with this needs its own path.
BROKER_RECOVERY_SECONDS = 900 + 110


def _celery_app():
    from base.celery import app
    return app


def _registered_tasks():
    """Force autodiscovery before reading the registry.

    `app.tasks` holds only Celery's built-ins plus whatever happens to have been
    imported, so reading it directly makes a test that passes or fails on import
    order. `import_default_modules` is what the durability suite uses for the same
    reason.
    """
    app = _celery_app()
    app.loader.import_default_modules()
    return app.tasks


# ---------------------------------------------------------------------------
# The default objective, and why it is what it is
# ---------------------------------------------------------------------------

def test_the_broker_recovery_objective_is_still_what_the_policy_assumed():
    """Every decision above was made against this number. If it moves, revisit them.

    Not a preference for 900 -- a check that the classification is not silently
    reasoning about a value that has changed.
    """
    opts = getattr(settings, 'CELERY_BROKER_TRANSPORT_OPTIONS', None) or {}
    vt = opts.get('visibility_timeout')

    assert vt == 900, (
        f'visibility_timeout is now {vt}, not the 900 s the recovery policy in this '
        f'file was written against. Re-read the classification: a longer timeout '
        f'may push another task past its objective, and a shorter one may breach '
        f'the 540 s floor and cause duplicate execution.'
    )


def test_the_floor_that_stops_us_lowering_it_is_unchanged():
    """The reason a global retune is not the answer."""
    assert settings.CELERY_TASK_TIME_LIMIT == 360
    assert getattr(settings, 'TRIP_ACTUALS_DELAY_SECONDS', 180) == 180

    floor = 180 + settings.CELERY_TASK_TIME_LIMIT
    opts = getattr(settings, 'CELERY_BROKER_TRANSPORT_OPTIONS', None) or {}
    assert opts.get('visibility_timeout', 0) > floor, (
        f'visibility_timeout no longer clears the {floor}s floor, so a message '
        f'still being worked on can be handed to a second worker'
    )


# ---------------------------------------------------------------------------
# auto_cancel_trip — the one task that could not live with the default
# ---------------------------------------------------------------------------

def test_the_accept_deadline_has_a_recovery_path_that_is_not_the_broker():
    """The classification's only TASK-SPECIFIC RECOVERY row.

    A rider whose accept deadline was on a dead worker must not wait a quarter of an
    hour to be told nobody accepted.
    """
    scheduled = {e['task'] for e in settings.CELERY_BEAT_SCHEDULE.values()}
    assert 'ride.sweep_unaccepted_trips' in scheduled


def test_the_accept_deadline_recovery_is_far_faster_than_the_broker():
    """The whole point of adding it. Assert the improvement, not just its presence."""
    entry = next(e for e in settings.CELERY_BEAT_SCHEDULE.values()
                 if e['task'] == 'ride.sweep_unaccepted_trips')
    # crontab(minute='*/2') -- read the minute field rather than guessing.
    minutes = getattr(entry['schedule'], '_orig_minute', None) or str(
        getattr(entry['schedule'], 'minute', ''))
    assert '*/2' in str(minutes) or '2' in str(minutes), (
        f'the sweep schedule is {minutes!r}; the policy assumes ~2 minutes'
    )

    sweep_objective = 2 * 60 + settings.TRIP_UNACCEPTED_SWEEP_GRACE_SECONDS
    assert sweep_objective < BROKER_RECOVERY_SECONDS / 3, (
        f'the durable sweep recovers in ~{sweep_objective}s against the broker\'s '
        f'~{BROKER_RECOVERY_SECONDS}s, which is not the order-of-magnitude '
        f'improvement the policy claims'
    )


def test_the_sweep_does_not_race_the_countdown_it_backs_up():
    """A backstop that fires first is not a backstop; it is a second opinion."""
    assert settings.TRIP_UNACCEPTED_SWEEP_GRACE_SECONDS > 0, (
        'without a grace period the sweep can cancel a trip whose countdown was '
        'about to fire, which makes the two mechanisms race'
    )


# ---------------------------------------------------------------------------
# The tasks whose recovery is an ALTERNATE PATH rather than redelivery
# ---------------------------------------------------------------------------

def test_sos_has_a_durable_record_independent_of_its_notification():
    """The SOS row is the recovery path; the notification is best-effort.

    §11's distinction, checked here because it is also §9's answer for this task: a
    lost SOS notification does not mean a lost SOS.
    """
    from servers.sos.models import SOSEvent

    assert hasattr(SOSEvent, 'status'), (
        'SOSEvent has no status field, so an operator cannot find an SOS whose '
        'notification never arrived'
    )
    field_names = {f.name for f in SOSEvent._meta.get_fields()}
    assert 'created_at' in field_names or 'raised_at' in field_names, (
        'an SOS with no timestamp cannot be ordered in an operator queue'
    )


def test_the_operator_can_find_an_sos_nobody_was_told_about():
    """An alternate recovery path that nobody can reach is not one."""
    from servers.sos import urls as sos_urls

    routes = {getattr(p, 'name', None) or str(getattr(p, 'pattern', ''))
              for p in sos_urls.urlpatterns}
    assert any('admin' in str(r) for r in routes), (
        'there is no operator listing for SOS events, so the durable record is not '
        'reachable and a lost notification IS a lost SOS'
    )


def test_the_receipt_has_a_resend_path():
    """Its classification says "tolerant, plus a resend endpoint"."""
    from servers.ride import urls as ride_urls

    patterns = {str(getattr(p, 'pattern', '')) for p in ride_urls.urlpatterns}
    assert any('receipt/resend' in p for p in patterns), (
        'no resend endpoint, so a receipt lost with its worker has no alternate '
        'path and its classification is wrong'
    )


# ---------------------------------------------------------------------------
# Queue separation: the decision not to do it, recorded with its trigger
# ---------------------------------------------------------------------------

def test_no_task_is_routed_to_a_queue_that_has_no_worker():
    """The failure mode of adopting queue separation carelessly.

    A task routed to a queue nothing consumes is not slow -- it never runs, and
    nothing reports it. If routing is ever introduced, this is the check that stops
    it being introduced silently.
    """
    conf = _celery_app().conf
    routes = conf.task_routes or {}

    assert not routes, (
        f'task routing has been introduced ({routes}). The recovery policy in this '
        f'file assumed a single default queue, and queue separation was explicitly '
        f'deferred. Update the classification, and make sure a worker consumes '
        f'every queue named here.'
    )


@pytest.mark.parametrize('task_name', [
    'servers.ride.tasks.auto_cancel_trip',
    'ride.sweep_unaccepted_trips',
    'ride.dispatch_wave',
    'sos.dispatch_sos',
    'base.utils.send_otp_via_sns',
    'auth_user.send_push_notification_task',
    'ride.issue_receipt_for_trip',
    'ride.persist_location_trail',
    'ride.compute_trip_actuals',
])
def test_every_classified_task_actually_exists(task_name):
    """A classification for a task that no longer exists is worse than none."""
    assert task_name in _registered_tasks(), (
        f'{task_name} is classified in this file but is not registered. Either it '
        f'was renamed -- in which case the policy now describes nothing -- or it '
        f'was removed and its row should go.'
    )
