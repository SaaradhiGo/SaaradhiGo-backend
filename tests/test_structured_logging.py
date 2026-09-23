"""Structured logging and PII redaction, including for Celery worker output.

Two defects motivated this:

1. `worker_hijack_root_logger` defaults to True, so Celery replaced the root
   logger's handlers and Django's `LOGGING` never governed worker output. The
   dispatch events added in ADR-0008 logged their event *name* but none of
   their fields, and no worker line passed through redaction. Confirmed during
   the PR 2 QA rehearsal: setting `DJANGO_LOG_FORMAT=json` on the worker
   changed nothing.

2. `PIIRedactionFilter` scrubbed only the formatted message. `JSONFormatter`
   promotes every `extra` key to a top-level JSON field, so structured logging
   was a channel PII could travel down untouched — the message filter cannot
   see values that are not in the message.
"""

import json
import logging

import pytest

from base.logging_filters import JSONFormatter, PIIRedactionFilter


def _record(msg='hello', level=logging.INFO, **extra):
    rec = logging.LogRecord(
        name='servers.ride.dispatch', level=level, pathname=__file__,
        lineno=1, msg=msg, args=(), exc_info=None,
    )
    for k, v in extra.items():
        setattr(rec, k, v)
    return rec


def _emit(msg='hello', **extra):
    """Run a record through the real filter then the real formatter."""
    rec = _record(msg, **extra)
    assert PIIRedactionFilter().filter(rec) is True
    return json.loads(JSONFormatter().format(rec))


# ---------------------------------------------------------------------------
# Structured dispatch fields survive
# ---------------------------------------------------------------------------

def test_dispatch_fields_are_promoted_to_json_keys():
    out = _emit(
        'dispatch_wave_started', event='dispatch_wave_started',
        trip_id=42, epoch='abc123', wave_index=1, radius=3000,
        candidates=4, delivered=4, pushes_queued=3,
    )
    assert out['msg'] == 'dispatch_wave_started'
    assert out['event'] == 'dispatch_wave_started'
    assert out['trip_id'] == 42
    assert out['wave_index'] == 1
    assert out['radius'] == 3000
    assert out['candidates'] == 4
    assert out['delivered'] == 4
    assert out['pushes_queued'] == 3
    assert out['logger'] == 'servers.ride.dispatch'
    assert out['lvl'] == 'INFO'


def test_retry_and_error_context_survives():
    out = _emit(
        'dispatch_wave_retry', event='dispatch_wave_retry',
        trip_id=7, wave_index=2, attempt=1, error='ConnectionError',
    )
    assert out['attempt'] == 1
    assert out['error'] == 'ConnectionError'
    assert out['trip_id'] == 7


def test_celery_task_lifecycle_messages_are_preserved():
    """Celery's own lines must still come through, just formatted by us."""
    rec = logging.LogRecord(
        name='celery.app.trace', level=logging.INFO, pathname=__file__, lineno=1,
        msg='Task ride.dispatch_wave[%s] succeeded in %ss', args=('abc', '0.09'),
        exc_info=None,
    )
    PIIRedactionFilter().filter(rec)
    out = json.loads(JSONFormatter().format(rec))
    assert 'ride.dispatch_wave' in out['msg']
    assert 'succeeded' in out['msg']
    assert out['logger'] == 'celery.app.trace'


# ---------------------------------------------------------------------------
# PII must not survive — message half (pre-existing behaviour)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('msg,must_not_contain', [
    ('otp=123456 for the trip', '123456'),
    ('Authorization: Bearer abcdef123456', 'abcdef123456'),
    ('rider +919876543210 called', '9876543210'),
])
def test_message_pii_is_redacted(msg, must_not_contain):
    out = _emit(msg)
    assert must_not_contain not in out['msg'], out['msg']


def test_jwt_in_message_is_redacted():
    jwt = 'eyJhbGciOiJIUzI1NiJ9.eyJ1c2VyX2lkIjoiMSJ9.abcdefghijklmnop'
    out = _emit(f'token was {jwt}')
    assert 'eyJhbGci' not in out['msg']
    assert '<jwt-redacted>' in out['msg']


# ---------------------------------------------------------------------------
# PII must not survive — structured extras (the new half)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('key', [
    'otp', 'trip_otp', 'phone', 'phone_number', 'driver_phone',
    'fcm_token', 'access_token', 'refresh_token', 'jwt',
    'password', 'webhook_secret', 'authorization', 'api_key', 'email',
])
def test_sensitive_extra_keys_are_redacted_by_name(key):
    out = _emit('event', **{key: 'SENSITIVE-VALUE-123456'})
    assert out[key] == '***', f'{key} leaked: {out[key]!r}'
    assert 'SENSITIVE-VALUE-123456' not in json.dumps(out)


@pytest.mark.parametrize('key', [
    'lat', 'lng', 'latitude', 'longitude',
    'pickup_lat', 'pickup_lng', 'pickup_long',
    'destination_lat', 'destination_long',
    'pickup_address', 'destination_address', 'location', 'coordinates',
])
def test_coordinate_and_address_extras_are_redacted(key):
    """A precise pickup point is location data about a named trip. `trip_id`
    is enough to look the trip up."""
    out = _emit('event', **{key: '17.3850'})
    assert out[key] == '***', f'{key} leaked'


@pytest.mark.parametrize('key', ['rider_name', 'driver_name', 'full_name', 'payee_name'])
def test_name_extras_are_redacted(key):
    out = _emit('event', **{key: 'Real Person'})
    assert out[key] == '***'
    assert 'Real Person' not in json.dumps(out)


def test_pii_nested_inside_an_extra_dict_is_redacted():
    out = _emit('event', payload={
        'trip_id': 9,
        'phone_number': '+919876543210',
        'nested': {'otp': '654321', 'radius': 1500},
    })
    blob = json.dumps(out)
    assert '9876543210' not in blob
    assert '654321' not in blob
    # Non-sensitive siblings survive.
    assert out['payload']['trip_id'] == 9
    assert out['payload']['nested']['radius'] == 1500


def test_pii_pattern_applies_to_extra_string_values():
    """Even under a harmless key name, a phone number in the value is scrubbed."""
    out = _emit('event', note='called +919876543210 twice')
    assert '9876543210' not in out['note']


def test_non_sensitive_dispatch_fields_are_untouched():
    out = _emit('dispatch_wave_offers', trip_id=1, wave_index=0, radius=1500,
                candidates=3, delivered=3, epoch='deadbeef', reason='status_cancelled')
    assert out['trip_id'] == 1
    assert out['radius'] == 1500
    assert out['candidates'] == 3
    assert out['epoch'] == 'deadbeef'
    assert out['reason'] == 'status_cancelled'


def test_filter_never_drops_a_record_even_on_bad_input():
    class Exploding:
        def __repr__(self):
            raise RuntimeError('boom')

    rec = _record('event', weird=Exploding())
    # Must return True (record kept) and must not raise.
    assert PIIRedactionFilter().filter(rec) is True


def test_formatter_survives_unserialisable_extra():
    class NotJson:
        def __repr__(self):
            return '<NotJson>'

    out = _emit('event', obj=NotJson(), trip_id=5)
    assert out['trip_id'] == 5
    assert out['obj'] == '<NotJson>'


# ---------------------------------------------------------------------------
# The Celery configuration itself
# ---------------------------------------------------------------------------

def test_celery_does_not_hijack_the_root_logger():
    """Negative control for the whole file.

    If this flips back to True, every assertion above becomes irrelevant for
    worker output: Celery would replace the root handler and neither the
    formatter nor the filter would run there.
    """
    from base.celery import app
    assert app.conf.worker_hijack_root_logger is False


def test_logging_config_attaches_the_redaction_filter_to_every_handler():
    from django.conf import settings

    handlers = settings.LOGGING['handlers']
    assert handlers, 'no handlers configured'
    for name, handler in handlers.items():
        assert 'pii_redact' in handler.get('filters', []), (
            f'handler {name!r} does not run the PII redaction filter'
        )


# ---------------------------------------------------------------------------
# Celery task arguments must never be logged verbatim
# ---------------------------------------------------------------------------

def test_celery_task_args_are_redacted_but_outcome_is_kept():
    """Celery embeds the full argument list in its own structured extras.

    `celery.worker.strategy` does it on "received" and `celery.app.trace` on
    "succeeded". Several tasks legitimately carry human names in their
    arguments — the push bodies are "New ride request from <rider>" and
    "Driver <name> has accepted your ride" — and the message patterns match
    phones, OTPs and JWTs, not names. Verified against a real worker: before
    this, the rider name appeared in worker output.

    Arguments are dropped; task name, id, runtime and return value survive,
    which is what these lines are actually for.
    """
    out = _emit(
        'Task auth_user.send_push_notification_task[abc] succeeded in 0.0s: False',
        data={
            'id': 'abc',
            'name': 'auth_user.send_push_notification_task',
            'args': "(424242, 'New Ride Request', 'New ride request from Ravi Kumar', {})",
            'kwargs': '{}',
            'return_value': 'False',
            'runtime': 0.0,
        },
    )
    assert out['data']['args'] == '***'
    assert out['data']['kwargs'] == '***'
    assert 'Ravi Kumar' not in json.dumps(out)
    # Still useful.
    assert out['data']['name'] == 'auth_user.send_push_notification_task'
    assert out['data']['return_value'] == 'False'
    assert out['data']['runtime'] == 0.0
    assert out['data']['id'] == 'abc'


def test_nested_args_key_is_redacted_at_any_depth():
    """Celery nests args one level down, inside its `data` extra."""
    out = _emit('event', data={'args': "('Ravi Kumar',)", 'name': 'some.task'})
    assert out['data']['args'] == '***'
    assert out['data']['name'] == 'some.task'
    assert 'Ravi Kumar' not in json.dumps(out)


def test_payment_card_keys_are_still_redacted():
    """Narrowing the 'card' substring must not stop redacting real card data."""
    import logging

    from base.logging_filters import PIIRedactionFilter

    f = PIIRedactionFilter()
    rec = logging.LogRecord('t', logging.INFO, __file__, 1, 'x', (), None)
    rec.card_number = '4111111111111111'
    rec.cardnumber = '4111111111111111'
    rec.card_no = '4111111111111111'
    rec.pan_number = 'ABCDE1234F'
    rec.cvv = '123'
    f.filter(rec)

    for attr in ('card_number', 'cardnumber', 'card_no', 'pan_number', 'cvv'):
        assert getattr(rec, attr) == '***', attr


def test_pricing_provenance_keys_survive_redaction():
    """rate_card_id / rate_card_version are audit provenance, not card data.

    Redacting them looked like privacy while destroying exactly the trail a fare
    audit needs -- found because a RateCard test asserted on a logged id and got
    '***' back.
    """
    import logging

    from base.logging_filters import PIIRedactionFilter

    f = PIIRedactionFilter()
    rec = logging.LogRecord('t', logging.INFO, __file__, 1, 'x', (), None)
    rec.rate_card_id = 17
    rec.rate_card_version = 3
    rec.zone_code = 'HYD'
    f.filter(rec)

    assert rec.rate_card_id == 17
    assert rec.rate_card_version == 3
    assert rec.zone_code == 'HYD'
