"""C1 — the kill-the-worker matrix, run against a real broker and real worker processes.

WHAT THIS IS FOR
----------------
`tests/test_celery_durability.py` asserts that `task_acks_late` and
`task_reject_on_worker_lost` are on. That is a configuration assertion, and the file
says so honestly. It does not prove that a worker killed mid-task actually gets its
work back, because that depends on the broker transport, not only on Celery.

This drill kills real worker processes and watches the broker. It needs no product
data and no QA deployment: it talks to a dedicated local Redis, so it can be run on
any machine without touching a shared instance.

    docker run -d --name sg-celery-redis -p 127.0.0.1:6390:6379 redis:7-alpine
    python qa/celery_worker_loss_drill.py

WHY THE BROKER MATTERS MORE THAN THE SETTING
-------------------------------------------
With the Redis transport a reserved-but-unacked message is not left on the queue. The
worker moves it into an `unacked` hash and records a deadline in `unacked_index`. If
that worker dies, the message is invisible to every other worker until the deadline
passes -- `visibility_timeout`, which defaults to **3600 seconds**.

So `acks_late` does not mean "redelivered promptly". It means "not lost". Those are
different promises, and for `auto_cancel_trip` the difference is a rider waiting an
hour past the accept timeout for a cancellation that was scheduled to fire in
ninety seconds.

The drill measures both halves:

  * the message survives the kill  -- observed directly in the broker keyspace;
  * when it comes back             -- measured empirically with a short visibility
                                      timeout, and read from configuration for the
                                      value the deployment actually runs with.

RUN THIS ON LINUX
-----------------
The first run of this drill on Windows reported the interrupted task as never
redelivered, and that was not a bug in the drill. `WorkController.should_use_eventloop`
(celery/worker/worker.py:239) ends with `and not self.app.IS_WINDOWS`, so a Windows
worker runs `synloop` instead of `asynloop`. `register_with_event_loop` is never
called, and that function is the only place the periodic
`cycle.maybe_restore_messages` is scheduled (kombu/transport/redis.py:1383).

The consequence is absolute rather than slow: **a Celery worker running on Windows
never restores a dead worker's reserved messages.** The message sits in the `unacked`
hash forever. Confirmed by calling `qos.restore_visible()` by hand against the same
broker, which restored it immediately.

Production runs Linux containers, so this is not a production defect. It is a hard
constraint on where a worker may run, and it is why this drill is invoked through the
project image:

    docker build -t sg-backend-drill .
    docker run --rm -v "$PWD:/App" -w /App sg-backend-drill         python qa/celery_worker_loss_drill.py --host host.docker.internal

A Windows run is still useful -- it proves the message survives the kill -- but it
cannot prove redelivery, and the platform is printed in the header so a result is
never read as more than it is.

NOTE ON THE KILL
----------------
On POSIX this sends SIGKILL. On Windows there is no SIGKILL; `Popen.kill()` calls
TerminateProcess, which likewise gives the process no chance to run a handler, flush,
or ack. Either way the worker does not get to say goodbye to the broker, which is the
condition under test. The platform used is recorded in the output so the evidence is
not overstated.
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

DEFAULT_BROKER_HOST = os.environ.get('DRILL_REDIS_HOST', '127.0.0.1')
DEFAULT_BROKER_PORT = int(os.environ.get('DRILL_REDIS_PORT', '6390'))
DEFAULT_BROKER_DB = int(os.environ.get('DRILL_REDIS_DB', '0'))

results = []


def record(stage, ok, detail):
    results.append({'stage': stage, 'ok': bool(ok), 'detail': detail})
    mark = 'PASS' if ok else 'FAIL'
    print(f'  [{mark}] {stage}: {detail}', flush=True)


def observe(stage, detail):
    """An observation, not an assertion. Recorded but never fails the drill."""
    results.append({'stage': stage, 'ok': None, 'detail': detail})
    print(f'  [obs ] {stage}: {detail}', flush=True)


# ---------------------------------------------------------------------------
# Broker
# ---------------------------------------------------------------------------

def broker_url(host, port, db):
    return f'redis://{host}:{port}/{db}'


def redis_client(host, port, db):
    import redis
    return redis.Redis(host=host, port=port, db=db, decode_responses=True)


def queue_depth(rc, queue):
    try:
        return rc.llen(queue)
    except Exception:
        return -1


def unacked_snapshot(rc):
    """What the broker is holding on behalf of workers that have not acked.

    `unacked` is a hash of delivery-tag -> message. `unacked_index` is a zset of
    delivery-tag scored by the time the message becomes visible again. Reading both
    is how the drill proves a killed worker's message still exists without waiting
    out the visibility timeout.
    """
    out = {'unacked': 0, 'unacked_index': 0, 'index_entries': []}
    try:
        out['unacked'] = rc.hlen('unacked')
        out['unacked_index'] = rc.zcard('unacked_index')
        out['index_entries'] = [
            {'tag': tag, 'visible_at': score}
            for tag, score in rc.zrange('unacked_index', 0, 4, withscores=True)
        ]
    except Exception as exc:
        out['error'] = repr(exc)
    return out


# ---------------------------------------------------------------------------
# Worker process
# ---------------------------------------------------------------------------

def worker_env(broker, run_log, visibility_timeout):
    env = os.environ.copy()
    # base.settings_ci is a thin wrapper that setdefaults the non-secret boot
    # values (SECRET_KEY, ALLOWED_HOSTS) and then imports base.settings unchanged.
    # Every Celery setting under test still comes from base.settings.
    env['DJANGO_SETTINGS_MODULE'] = 'base.settings_ci'
    env['DEBUG_ENV'] = 'True'
    env['REDIS_URL'] = broker.rsplit('/', 1)[0]
    env['CELERY_PROBE_RUN_LOG'] = str(run_log)
    # The worker must agree with the enqueuer about the timeout, or the
    # replacement worker would use the deployment default and the drill would
    # sit for an hour. settings.py reads this env var.
    if visibility_timeout:
        env['CELERY_VISIBILITY_TIMEOUT_SECONDS'] = str(int(visibility_timeout))
    env['PYTHONPATH'] = str(REPO)
    env['PYTHONUNBUFFERED'] = '1'
    return env


def start_worker(broker, queue, run_log, visibility_timeout, log_path):
    """A real `celery worker` subprocess.

    Solo pool deliberately: with prefork, killing the parent leaves an orphaned
    child that may still finish and ack the task, which would make the kill a
    graceful shutdown wearing a disguise. Solo puts the executing task in the
    process being killed.
    """
    cmd = [
        sys.executable, '-m', 'celery', '-A', 'base', 'worker',
        '-l', 'INFO', '-P', 'solo', '--concurrency', '1',
        '-Q', queue, '-n', f'drill-{uuid.uuid4().hex[:6]}@%h',
        '--include', 'tests.celery_probe_tasks',
        '--without-gossip', '--without-mingle', '--without-heartbeat',
    ]
    fh = open(log_path, 'ab')
    proc = subprocess.Popen(
        cmd, cwd=str(REPO), env=worker_env(broker, run_log, visibility_timeout),
        stdout=fh, stderr=subprocess.STDOUT,
    )
    proc._drill_log = fh                      # keep the handle alive
    return proc


def wait_for_worker_ready(log_path, timeout=60):
    """Wait for the banner. Starting Django + Celery cold takes a few seconds."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            text = Path(log_path).read_text('utf-8', 'ignore')
        except OSError:
            text = ''
        if 'ready.' in text or 'celery@' in text and 'ready' in text:
            return True
        time.sleep(0.3)
    return False


def kill_hard(proc):
    """No handler, no flush, no ack. See the module docstring on platforms."""
    if proc.poll() is not None:
        return 'already_exited'
    if hasattr(signal, 'SIGKILL') and os.name != 'nt':
        os.kill(proc.pid, signal.SIGKILL)
        how = 'SIGKILL'
    else:
        proc.kill()
        how = 'TerminateProcess'
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        pass
    return how


def stop_gracefully(proc):
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=20)
    except Exception:
        proc.kill()
    finally:
        try:
            proc._drill_log.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Run log
# ---------------------------------------------------------------------------

def markers(run_log):
    try:
        return [ln.strip() for ln in Path(run_log).read_text('utf-8', 'ignore').splitlines()
                if ln.strip()]
    except OSError:
        return []


def wait_for_marker(run_log, prefix, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if any(m.startswith(prefix) for m in markers(run_log)):
            return True
        time.sleep(0.2)
    return False


def count_marker(run_log, prefix):
    return sum(1 for m in markers(run_log) if m.startswith(prefix))


# ---------------------------------------------------------------------------
# Enqueue, in-process, with the same app the worker loads
# ---------------------------------------------------------------------------

def make_app(broker, visibility_timeout):
    os.environ['DJANGO_SETTINGS_MODULE'] = 'base.settings_ci'
    os.environ['DEBUG_ENV'] = 'True'
    os.environ['REDIS_URL'] = broker.rsplit('/', 1)[0]
    if visibility_timeout:
        os.environ['CELERY_VISIBILITY_TIMEOUT_SECONDS'] = str(int(visibility_timeout))
    import django
    django.setup()
    from base.celery import app
    if visibility_timeout:
        app.conf.broker_transport_options = {
            **(app.conf.broker_transport_options or {}),
            'visibility_timeout': int(visibility_timeout),
        }
    return app


# ---------------------------------------------------------------------------
# The drill
# ---------------------------------------------------------------------------

def drill(host, port, db, visibility_timeout, workdir):
    broker = broker_url(host, port, db)
    queue = f'drill-{uuid.uuid4().hex[:8]}'
    run_log = Path(workdir) / f'{queue}-runs.log'
    w1_log = Path(workdir) / f'{queue}-worker1.log'
    w2_log = Path(workdir) / f'{queue}-worker2.log'
    run_log.write_text('', encoding='utf-8')

    rc = redis_client(host, port, db)
    try:
        rc.ping()
    except Exception as exc:
        record('BROKER_REACHABLE', False, f'{broker} unreachable: {exc!r}')
        return
    record('BROKER_REACHABLE', True, f'{broker}, dbsize={rc.dbsize()}')

    app = make_app(broker, visibility_timeout)
    import tests.celery_probe_tasks as probe            # noqa: F401  (registers)

    configured_vt = (app.conf.broker_transport_options or {}).get('visibility_timeout')
    observe(
        'CONFIGURED_VISIBILITY_TIMEOUT',
        f'{configured_vt if configured_vt is not None else "unset -> kombu default 3600"} s'
        f' (drill overrides to {visibility_timeout} s to make redelivery observable)',
    )

    # -----------------------------------------------------------------------
    # Control: a worker that is not killed executes the task exactly once.
    # -----------------------------------------------------------------------
    print('\n-- control: undisturbed execution', flush=True)
    proc = start_worker(broker, queue, run_log, visibility_timeout, w1_log)
    ready = wait_for_worker_ready(w1_log)
    record('WORKER_STARTS', ready, 'worker reported ready' if ready
           else f'no ready banner in 60 s; see {w1_log}')
    if not ready:
        stop_gracefully(proc)
        return

    token = f'control-{uuid.uuid4().hex[:6]}'
    probe.quick_task.apply_async((token,), queue=queue)
    got = wait_for_marker(run_log, f'quick:{token}', timeout=45)
    record('CONTROL_TASK_RUNS', got,
           'quick_task executed' if got else 'quick_task never executed')
    time.sleep(1.0)
    record('CONTROL_TASK_RUNS_ONCE', count_marker(run_log, f'quick:{token}') == 1,
           f'{count_marker(run_log, f"quick:{token}")} execution(s)')
    record('CONTROL_QUEUE_DRAINS', queue_depth(rc, queue) == 0,
           f'queue depth {queue_depth(rc, queue)} after completion')

    # -----------------------------------------------------------------------
    # The kill.
    # -----------------------------------------------------------------------
    print('\n-- kill the worker mid-execution', flush=True)
    token = f'killed-{uuid.uuid4().hex[:6]}'
    probe.slow_task.apply_async((token, 25.0), queue=queue)

    started = wait_for_marker(run_log, f'start:{token}', timeout=45)
    record('TASK_REACHES_THE_WORKER', started,
           'slow_task began executing' if started else 'slow_task never started')
    if not started:
        stop_gracefully(proc)
        return

    before = unacked_snapshot(rc)
    observe('BROKER_STATE_WHILE_EXECUTING',
            f'queue depth {queue_depth(rc, queue)}, unacked={before["unacked"]}, '
            f'unacked_index={before["unacked_index"]}')
    record('RESERVED_MESSAGE_IS_HELD_UNACKED', before['unacked'] >= 1,
           f'unacked hash holds {before["unacked"]} message(s) — the task is '
           f'executing and has NOT been acknowledged')

    how = kill_hard(proc)
    killed_at = time.time()
    record('WORKER_DIES_MID_TASK', proc.poll() is not None,
           f'{how}, exit={proc.poll()}, before the finish marker was written')
    record('TASK_DID_NOT_COMPLETE', count_marker(run_log, f'finish:{token}') == 0,
           f'{count_marker(run_log, f"finish:{token}")} finish marker(s) — the '
           f'work was genuinely interrupted')

    after = unacked_snapshot(rc)
    record('MESSAGE_SURVIVES_THE_KILL', after['unacked'] >= 1,
           f'unacked hash still holds {after["unacked"]} message(s) after the '
           f'worker died — the task was not lost')
    if after['index_entries']:
        e = after['index_entries'][0]
        observe('REDELIVERY_DEADLINE',
                f'visible again at epoch {e["visible_at"]:.0f}, i.e. '
                f'{e["visible_at"] - killed_at:.0f} s from the kill')

    # -----------------------------------------------------------------------
    # Restart, and time the redelivery.
    # -----------------------------------------------------------------------
    print('\n-- restart the worker and wait for redelivery', flush=True)
    proc2 = start_worker(broker, queue, run_log, visibility_timeout, w2_log)
    ready = wait_for_worker_ready(w2_log)
    record('REPLACEMENT_WORKER_STARTS', ready, 'second worker ready' if ready
           else f'no ready banner; see {w2_log}')

    # The budget is visibility_timeout PLUS the polling granularity, which the
    # first run of this drill discovered the hard way.
    #
    # kombu/transport/redis.py:410 -- restore_visible() increments a counter and
    # returns early unless `(count - 1) % interval == 0`, with interval defaulting
    # to 10. And the event loop calls maybe_restore_messages() every 10 seconds
    # (redis.py:1383). So a worker makes a real restore attempt roughly every 100
    # seconds, not every 10.
    #
    # Recovery time for a lost task is therefore visibility_timeout + up to ~100 s,
    # not visibility_timeout. A 45 s budget failed this assertion against behaviour
    # that was working correctly.
    RESTORE_POLL_GRANULARITY = 110
    budget = int(visibility_timeout) + RESTORE_POLL_GRANULARITY
    redelivered = False
    deadline = time.time() + budget
    while time.time() < deadline:
        if count_marker(run_log, f'start:{token}') >= 2:
            redelivered = True
            break
        time.sleep(0.5)
    elapsed = time.time() - killed_at

    record('TASK_IS_REDELIVERED', redelivered,
           f'the interrupted task ran again {elapsed:.0f} s after the kill'
           if redelivered else
           f'NOT redelivered within {budget} s of the kill')

    if redelivered:
        observe('REDELIVERY_LATENCY',
                f'{elapsed:.0f} s with visibility_timeout={visibility_timeout} s — '
                f'the wait is the visibility timeout plus restore-poll granularity, '
                f'not the task duration')
        # Asserted, not just observed: if a kombu upgrade changed the restore
        # cadence, recovery time would move and nothing else would notice.
        record('REDELIVERY_IS_BOUNDED_BY_THE_VISIBILITY_TIMEOUT',
               elapsed >= int(visibility_timeout) - 2,
               f'redelivery took {elapsed:.0f} s, at or beyond the '
               f'{visibility_timeout} s timeout — it is the timeout that gates '
               f'recovery, so the configured value is the recovery SLA')
        done = wait_for_marker(run_log, f'finish:{token}', timeout=60)
        record('REDELIVERED_TASK_COMPLETES', done,
               'the second execution ran to completion'
               if done else 'the redelivered task did not finish')

    stop_gracefully(proc2)
    observe('RUN_LOG', str(run_log))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--host', default=DEFAULT_BROKER_HOST)
    ap.add_argument('--port', type=int, default=DEFAULT_BROKER_PORT)
    ap.add_argument('--db', type=int, default=DEFAULT_BROKER_DB)
    ap.add_argument(
        '--visibility-timeout', type=int, default=30,
        help='Overridden for the drill so redelivery is observable. The '
             'deployment default is 3600 s; see the report.')
    ap.add_argument('--workdir', default=os.environ.get('TEMP', '.'))
    ap.add_argument('--json', action='store_true')
    args = ap.parse_args()

    print(f'C1 kill-the-worker matrix — platform={sys.platform}', flush=True)
    drill(args.host, args.port, args.db, args.visibility_timeout, args.workdir)

    asserted = [r for r in results if r['ok'] is not None]
    passed = sum(1 for r in asserted if r['ok'])
    print(f'\n{passed}/{len(asserted)} assertions passed '
          f'({sum(1 for r in results if r["ok"] is None)} observations)', flush=True)
    if args.json:
        print(json.dumps(results, indent=2))
    return 0 if passed == len(asserted) else 1


if __name__ == '__main__':
    sys.exit(main())
