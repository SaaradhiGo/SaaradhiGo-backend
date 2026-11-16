"""Messages to trusted contacts, on the provider the login code already uses (AWS SNS).

    SHARING_SMS_MODE = sink (the default, and QA's) records each message as `sink` and sends
    nothing: the QA test SMS sink. `live` hands it to `base.utils.send_otp_via_sns`.

Rate limits per rider and per contact, counted from the log. An SOS is never held by the
rider's limit (only by the per-contact one, so a stuck button cannot flood one phone). A
number that replied STOP gets nothing. The log keeps the delivery status only, never the text.
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from .models import ContactSms, SmsOptOut

logger = logging.getLogger(__name__)

STOP_WORDS = {'STOP', 'STOPALL', 'UNSUBSCRIBE', 'CANCEL', 'END', 'QUIT'}


def mode():
    return (getattr(settings, 'SHARING_SMS_MODE', 'sink') or 'sink').strip().lower()


def _over_limit(rider, contact, kind):
    hour_ago = timezone.now() - timedelta(hours=1)
    counted = ContactSms.objects.filter(created_at__gte=hour_ago).exclude(
        status__in=('rate_limited', 'opted_out'))
    per_contact = int(getattr(settings, 'SHARING_SMS_PER_CONTACT_PER_HOUR', 6))
    if counted.filter(contact=contact).count() >= per_contact:
        return True
    if kind == 'sos':
        return False
    per_rider = int(getattr(settings, 'SHARING_SMS_PER_RIDER_PER_HOUR', 20))
    return counted.filter(rider=rider).count() >= per_rider


def opted_out(phone):
    return SmsOptOut.objects.filter(phone=phone).exists()


def send(rider, contact, kind, text, trip_id=None, template=None):
    """Send (or sink) one message. Returns True when it went to the provider or the sink."""
    if opted_out(contact.phone):
        status = 'opted_out'
    elif _over_limit(rider, contact, kind):
        status = 'rate_limited'
    elif mode() == 'live':
        try:
            from base.utils import send_otp_via_sns, stash_sms_message

            tkind, tvalues = template or ('', None)
            send_otp_via_sns.delay(contact.phone, stash_sms_message(
                text, ttl=3600, kind=tkind, values=tvalues))
            status = 'queued'
        except Exception:  # noqa: BLE001 - a failed send is logged, never raised into a trip
            logger.exception('contact_sms_failed kind=%s contact=%s', kind, contact.pk)
            status = 'failed'
    else:
        status = 'sink'
    ContactSms.objects.create(rider=rider, contact=contact, trip_id=trip_id, kind=kind,
                              status=status)
    logger.info('contact_sms kind=%s contact=%s status=%s', kind, contact.pk, status)
    return status in ('queued', 'sink')


def record_inbound(phone, body):
    """An inbound reply. STOP (and its usual synonyms) opts the number out for good."""
    if (body or '').strip().upper() in STOP_WORDS:
        SmsOptOut.objects.get_or_create(phone=phone)
        return True
    return False
