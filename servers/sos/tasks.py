"""SOS dispatch fan-out (runs in Celery)."""

import logging

from celery import shared_task

logger = logging.getLogger(__name__)

# How long the emergency-contact SMS text waits in the cache for the send task. Longer than
# a login OTP's: a backed-up queue should delay this message, not lose it.
SOS_SMS_TTL_SECONDS = 3600


@shared_task(name='sos.dispatch_sos', bind=True, max_retries=2, default_retry_delay=10)
def dispatch_sos(self, sos_id):
    """Fan an SOS event out to ops + the caller's emergency contact.

    Three channels:
      1. FCM push to the operators holding `safety.respond` at the event's
         anchor (Global holders included; an unanchored event goes to every
         holder). It used to go to every staff user with the legacy role
         'admin' in every city, which paged Austin for Hyderabad. Reuses the
         async push facade so we don't block.
      2. SMS to the caller's emergency_contact (if set), via the
         existing SNS helper. Best-effort.
      3. (Future) Real-time event on /ops/sos/live WebSocket — wired
         when the ops console is built.

    Failures in any channel are logged but don't fail the task: the
    SOSEvent row itself is the durable record, and the rider already
    got their ack from the API. We retry the whole task at most twice
    in case of full-channel outage.
    """
    from servers.sos.models import SOSEvent
    from servers.auth_user.services import send_push_notification
    from servers.safety.scope import responders_for

    try:
        event = SOSEvent.objects.select_related('user', 'trip').get(id=sos_id)
    except SOSEvent.DoesNotExist:
        logger.warning(f"dispatch_sos: SOSEvent {sos_id} vanished")
        return False

    title = "SOS — immediate response required"
    body_lines = [
        f"SOS #{event.id} ({event.event_type}) by {event.user_label or 'unknown'}",
    ]
    if event.trip_id:
        body_lines.append(f"Trip #{event.trip_id}")
    if event.latitude and event.longitude:
        body_lines.append(f"Loc: {event.latitude},{event.longitude}")
    body = " | ".join(body_lines)

    # 1. Ops on-call push.
    try:
        ops_users = [
            u for u in responders_for(event.anchor_node) if u.fcm_token
        ]
        notified = 0
        for u in ops_users:
            if send_push_notification(
                u, title, body, {'type': 'sos', 'sos_id': str(event.id)}
            ):
                notified += 1
        logger.warning(
            f"dispatch_sos[{sos_id}]: notified {notified} ops users"
        )
    except Exception as e:
        logger.error(f"dispatch_sos[{sos_id}]: ops fan-out failed: {e}")

    # 2. SMS to caller's emergency contact (if any).
    try:
        emergency = getattr(event.user, 'emergency_contact', None) if event.user else None
        if emergency:
            from base.utils import send_otp_via_sns, stash_sms_message
            # The task takes a reference, never the text (base.utils explains).
            from base import sms_templates
            values = {'name': event.user_label or 'A user', 'sos_id': event.id}
            send_otp_via_sns.delay(
                emergency,
                stash_sms_message(
                    sms_templates.render('sos_emergency', **values),
                    ttl=SOS_SMS_TTL_SECONDS, kind='sos_emergency', values=values,
                ),
            )
            logger.warning(
                f"dispatch_sos[{sos_id}]: SMS queued to emergency contact"
            )
    except Exception as e:
        logger.error(f"dispatch_sos[{sos_id}]: emergency SMS failed: {e}")

    # 3. The rider's trusted contacts (servers/sharing): the live trip link, by SMS.
    try:
        if event.trip_id and event.initiated_by == 'rider' and event.user:
            from servers.sharing.services import on_sos
            sent = on_sos(event.trip, event.user)
            logger.warning(f"dispatch_sos[{sos_id}]: trip link sent to {sent} trusted contacts")
    except Exception as e:
        logger.error(f"dispatch_sos[{sos_id}]: trusted contact SMS failed: {e}")

    return True
