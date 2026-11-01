"""The dispatcher: `notify()` writes the outbox, `deliver()` sends one row.

    notify(event, user, context, channels=None, idempotency_key=...)
        resolves the active templates for the user's country (and channels), applies the
        recipient's preferences, the promotional quiet hours and the test allowlist, and
        writes one `NotificationMessage` per channel. Rows that may go are scheduled to send
        ON TRANSACTION COMMIT; rows that may not are recorded as `suppressed` with the reason,
        so "did the rider get the message?" always has an answer.

    deliver(message_id)
        claims the row (a conditional UPDATE, so a redelivered task cannot send twice), asks the
        adapter, and records the result. Transient failures retry with backoff up to
        NOTIFY_MAX_ATTEMPTS and then dead-letter as `failed`; permanent failures fail at once.

THE FLAG, AGAIN
---------------
`config.effective_provider` decides the provider for every send, at notify time AND again at
send time. With NOTIFY_LIVE_SEND off that is always `stub`, and `providers.build` would refuse
a live adapter even if asked. A live send outside production additionally requires the
recipient to be on NOTIFY_TEST_RECIPIENTS.
"""

import hashlib
import logging
import time
from datetime import timedelta

from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.db.models import F, Q
from django.utils import timezone

from . import config, preferences, quiet, recipients, templating
from .models import (
    DELIVERED, FAILED, IN_APP, PROMOTIONAL, QUEUED, SENDING, SENT, SUPPRESSED,
    NotificationMessage, NotificationStatusEvent, NotificationTemplate, TemplateRegistration,
    CHANNELS,
)
from .providers import build
from .providers.base import (
    Outbound, PermanentProviderError, TransientProviderError, mask_for_log,
)

logger = logging.getLogger(__name__)

LOCALE = 'en'


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

def record_event(message, status, *, source='dispatcher', detail='', provider=None,
                 provider_message_id=None):
    """Append one status event. Returns the event, or None if it was already recorded."""
    try:
        with transaction.atomic():
            return NotificationStatusEvent.objects.create(
                message=message, status=status, source=source, detail=(detail or '')[:500],
                provider=message.provider if provider is None else provider,
                provider_message_id=(message.provider_message_id if provider_message_id is None
                                     else provider_message_id))
    except IntegrityError:
        return None


# ---------------------------------------------------------------------------
# Resolving and deciding
# ---------------------------------------------------------------------------

def resolve_template(key, channel, country, locale=LOCALE):
    qs = NotificationTemplate.objects.filter(key=key, channel=channel, locale=locale,
                                             status='active')
    return qs.filter(country=country or '__none__').first() or qs.filter(country='').first()


def channels_with_template(key, country, locale=LOCALE):
    return [c for c in CHANNELS if resolve_template(key, c, country, locale) is not None]


SNS_NOT_CONFIRMED = 'sns not confirmed'


class Decision:
    def __init__(self, status, reason='', provider='', not_before=None):
        self.status = status
        self.reason = reason
        self.provider = provider
        self.not_before = not_before


def decide(user, *, channel, category, country, now=None, zone=None):
    """May this message go to this user on this channel now? (preferences, allowlist, hours)."""
    now = now or timezone.now()
    provider, live = config.effective_provider(channel, country)
    if config.configured_provider(channel, country) is None:
        return Decision(SUPPRESSED, f'no provider configured for {channel} in {country}', provider)

    # AWS SNS is sandbox-only until the owner confirms the account: no send, no network call.
    if live and config.sns_blocked(provider):
        return Decision(SUPPRESSED, SNS_NOT_CONFIRMED, provider)

    allowed, reason = preferences.is_opted_in(user.pk, channel, category)
    if not allowed:
        return Decision(SUPPRESSED, reason, provider)

    if channel != IN_APP and not recipients.address_for(user, channel):
        return Decision(SUPPRESSED, f'no {recipients.CHANNEL_ADDRESS[channel]} on file', provider)

    if live and not config.is_production():
        if not config.on_allowlist(user.phone_number, user.email):
            return Decision(SUPPRESSED, config.ALLOWLIST_SUPPRESSION, provider)

    not_before = None
    if category == PROMOTIONAL:
        not_before = quiet.release_time(now, zone or recipients.timezone_of(user, country), country)
    return Decision(QUEUED, '', provider, not_before)


# ---------------------------------------------------------------------------
# Writing the outbox
# ---------------------------------------------------------------------------

def _schedule(message_id, countdown=None):
    """Hand a row to Celery after the surrounding transaction commits. Never raises."""
    def go():
        try:
            from .tasks import send_message
            if countdown:
                send_message.apply_async(args=[message_id], countdown=countdown)
            else:
                send_message.delay(message_id)
        except Exception:  # noqa: BLE001 - the beat sweep (dispatch_queued) is the backstop
            logger.exception('notification_schedule_failed message_id=%s', message_id)

    transaction.on_commit(go)


def _hash(text):
    return hashlib.sha256((text or '').encode('utf-8')).hexdigest()


def enqueue(user, template, context, *, idempotency_key, source, campaign=None, now=None,
            country=None, zone=None, city=None):
    """Write (or find) the message row for one user on one template's channel."""
    now = now or timezone.now()
    key = f'{idempotency_key}:{template.channel}'[:200]
    existing = NotificationMessage.objects.filter(idempotency_key=key).first()
    if existing is not None:
        return existing

    country = country or recipients.country_of(user) or template.country
    base = dict(
        template=template, template_key=template.key, template_version=template.version,
        channel=template.channel, category=template.category, country=country or '',
        city=city if city is not None else recipients.city_of(user),
        recipient_user_id=user.pk, recipient_masked=recipients.masked_for(user, template.channel),
        source=(source or template.key)[:120], campaign=campaign, idempotency_key=key)
    try:
        body = templating.render(template.body, context)
        subject = templating.render(template.subject, context) if template.subject else ''
        title = templating.render(template.title, context) if template.title else ''
    except templating.MissingVariable as exc:
        return _create(base, Decision(FAILED, '', config.effective_provider(
            template.channel, country)[0]), error=str(exc))

    decision = decide(user, channel=template.channel, category=template.category,
                      country=country, now=now, zone=zone)
    base.update(subject=subject, title=title, body=body, body_hash=_hash(body),
                variable_values=templating.ordered_values(template.body, context))
    return _create(base, decision)


def _create(fields, decision, error=''):
    try:
        with transaction.atomic():
            message = NotificationMessage.objects.create(
                **fields, provider=decision.provider, status=decision.status,
                suppression_reason=decision.reason if decision.status == SUPPRESSED else '',
                last_error=error[:500], not_before=decision.not_before)
            record_event(message, decision.status, source='dispatcher',
                         detail=decision.reason or error, provider='', provider_message_id='')
            if decision.status == SUPPRESSED or decision.status == FAILED:
                _scrub(message, final=True)
            elif decision.not_before is None:
                _schedule(message.pk)
            else:
                delay = max(1, int((decision.not_before - timezone.now()).total_seconds()))
                _schedule(message.pk, countdown=delay)
    except IntegrityError:
        # Lost a race to another writer with the same idempotency key: theirs is the answer.
        return NotificationMessage.objects.get(idempotency_key=fields['idempotency_key'])
    return message


def notify(event, user, context, channels=None, *, idempotency_key, source=None, campaign=None,
           now=None, category=None):
    """Queue `event` for `user` on every channel that has an active template.

    `event` is the template key (for example `commission.increase_notice`). `channels` limits
    it. `category`, when given, restricts to templates of that category (a campaign passes
    promotional). Returns the message rows (created now, or found by the idempotency key).
    """
    if not idempotency_key:
        raise ValueError('idempotency_key is required')
    country = recipients.country_of(user)
    if country is None:
        logger.warning('notify skipped: recipient has no country event=%s user=%s', event, user.pk)
        return []
    rows = []
    for channel in (channels or CHANNELS):
        template = resolve_template(event, channel, country)
        if template is None:
            continue
        if category and template.category != category:
            continue
        rows.append(enqueue(user, template, context, idempotency_key=idempotency_key,
                            source=source or event, campaign=campaign, now=now, country=country))
    if not rows:
        logger.warning('notify: no active template event=%s country=%s channels=%s',
                       event, country, list(channels or CHANNELS))
    return rows


# ---------------------------------------------------------------------------
# Sending one row
# ---------------------------------------------------------------------------

def _scrub(message, final):
    """After a message is final: drop what only the send needed."""
    fields = []
    if message.variable_values:
        message.variable_values = []
        fields.append('variable_values')
    template = message.template
    if final and template is not None and template.has_personal_variables:
        for name in ('body', 'subject', 'title'):
            if getattr(message, name):
                setattr(message, name, '')
                fields.append(name)
    if fields:
        message.save(update_fields=fields + ['updated_at'])


def _rate_ok(provider):
    limit = config.rate_limit(provider)
    if not limit:
        return True
    try:
        key = f'notify:rate:{provider}:{int(time.time() // 60)}'
        cache.add(key, 0, 120)
        return cache.incr(key) <= limit
    except Exception:  # noqa: BLE001 - a cache outage must not stop notifications
        return True


def _registration_for(message, provider):
    if message.template_id is None:
        return {}
    reg = TemplateRegistration.objects.filter(template_id=message.template_id,
                                              provider=provider).first()
    if reg is None:
        return {}
    return {'sender_id': reg.sender_id, 'dlt_template_id': reg.dlt_template_id,
            'entity_id': reg.entity_id, 'provider_template_id': reg.provider_template_id,
            'whatsapp_template_name': reg.whatsapp_template_name,
            'whatsapp_namespace': reg.whatsapp_namespace}


def _finish(message, status, *, detail='', error=''):
    NotificationMessage.objects.filter(pk=message.pk).update(
        status=status, last_error=error[:500], updated_at=timezone.now(),
        sent_at=timezone.now() if status in (SENT, DELIVERED) else message.sent_at)
    message.refresh_from_db()
    record_event(message, status, source='dispatcher', detail=detail or error)
    _scrub(message, final=True)


def deliver(message_id, now=None):
    """Send one queued row. Returns the outcome word (used by the task and the tests)."""
    from django.contrib.auth import get_user_model

    now = now or timezone.now()
    claimed = (NotificationMessage.objects
               .filter(pk=message_id, status=QUEUED)
               .filter(Q(not_before__isnull=True) | Q(not_before__lte=now))
               .update(status=SENDING, attempts=F('attempts') + 1, updated_at=now))
    if not claimed:
        return 'skipped'
    message = NotificationMessage.objects.select_related('template').get(pk=message_id)
    user = get_user_model().objects.filter(pk=message.recipient_user_id).first()
    if user is None:
        _finish(message, SUPPRESSED, detail='recipient no longer exists')
        NotificationMessage.objects.filter(pk=message.pk).update(
            suppression_reason='recipient no longer exists')
        return 'suppressed'

    # Re-decide at send time: a preference, the flag or the allowlist may have changed since.
    decision = decide(user, channel=message.channel, category=message.category,
                      country=message.country, now=now)
    if decision.status == SUPPRESSED:
        NotificationMessage.objects.filter(pk=message.pk).update(
            suppression_reason=decision.reason[:200], provider=decision.provider)
        _finish(message, SUPPRESSED, detail=decision.reason)
        return 'suppressed'
    if decision.not_before is not None:
        NotificationMessage.objects.filter(pk=message.pk).update(
            status=QUEUED, attempts=F('attempts') - 1, not_before=decision.not_before)
        _schedule(message.pk, countdown=max(1, int((decision.not_before - now).total_seconds())))
        return 'held'
    if decision.provider != message.provider:
        NotificationMessage.objects.filter(pk=message.pk).update(provider=decision.provider)
        message.provider = decision.provider

    if not _rate_ok(message.provider):
        later = now + timedelta(seconds=60)
        NotificationMessage.objects.filter(pk=message.pk).update(
            status=QUEUED, attempts=F('attempts') - 1, not_before=later)
        _schedule(message.pk, countdown=60)
        return 'rate_limited'

    record_event(message, SENDING, detail=f'attempt {message.attempts}')
    try:
        adapter = build(message.provider, message.channel)
        outbound = Outbound(
            message_id=message.pk, channel=message.channel, category=message.category,
            country=message.country, user_id=user.pk,
            to_phone=user.phone_number if message.channel in ('sms', 'whatsapp') else '',
            to_email=user.email or '' if message.channel == 'email' else '',
            push_token=recipients.push_token_of(user) if message.channel == 'push' else '',
            subject=message.subject, title=message.title, body=message.body,
            values=list(message.variable_values or []),
            registration=_registration_for(message, message.provider),
            data={'variable_names': templating.placeholders(
                message.template.body) if message.template_id else []})
        result = adapter.send(outbound)
    except TransientProviderError as exc:
        return _retry_or_fail(message, str(exc), now)
    except PermanentProviderError as exc:
        _finish(message, FAILED, error=mask_for_log(str(exc)))
        return 'failed'
    except Exception as exc:  # noqa: BLE001 - treat the unknown as retryable, bounded by attempts
        logger.exception('notification_send_error message_id=%s', message.pk)
        return _retry_or_fail(message, f'{type(exc).__name__}', now)

    NotificationMessage.objects.filter(pk=message.pk).update(
        provider_message_id=result.provider_message_id[:200])
    message.provider_message_id = result.provider_message_id[:200]
    _finish(message, result.status, detail='accepted by provider')
    return result.status


def _retry_or_fail(message, error, now):
    error = mask_for_log(error)
    message.refresh_from_db()
    if message.attempts >= config.max_attempts():
        _finish(message, FAILED, error=f'dead-lettered after {message.attempts} attempts: {error}')
        return 'dead_letter'
    delay = config.backoff_seconds(message.attempts)
    NotificationMessage.objects.filter(pk=message.pk).update(
        status=QUEUED, last_error=error[:500], not_before=now + timedelta(seconds=delay),
        updated_at=now)
    record_event(message, QUEUED, detail=f'retry in {delay}s: {error}')
    _schedule(message.pk, countdown=delay)
    return 'retry'


# ---------------------------------------------------------------------------
# Status from webhooks
# ---------------------------------------------------------------------------

#: A webhook may move a message to these, from these.
_ADVANCE = {
    DELIVERED: (QUEUED, SENDING, SENT, FAILED),
    FAILED: (SENT, SENDING),
    SENT: (QUEUED, SENDING),
}


def apply_status(message, status, *, provider, provider_message_id, detail=''):
    """Record a provider-reported status. Idempotent per (provider id, status).

    Returns True when the event was new."""
    event = record_event(message, status, source=f'webhook:{provider}', detail=detail,
                         provider=provider, provider_message_id=provider_message_id)
    if event is None:
        return False
    allowed_from = _ADVANCE.get(status)
    if allowed_from:
        NotificationMessage.objects.filter(pk=message.pk, status__in=allowed_from).update(
            status=status, updated_at=timezone.now(),
            last_error=detail[:500] if status == FAILED else '')
    return True
