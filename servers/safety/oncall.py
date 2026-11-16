"""The SOS escalation chain (12 Nov, console QA): phone people until somebody acknowledges.

After the country's `sos_ack_minutes` an unacknowledged SOS pages the city's on-call PRIMARY, one
interval later the BACKUP, then the CITY HEAD, then the primary again, round after round, one
page per interval, until the SOS is acknowledged (or resolved). Each page is a call through the
call bridge (`callbridge.providers.get_provider().page`), with an SMS when the call cannot be
placed. Every page is a `SOSPage` row, a line on the incident's timeline and an audit row.

The city is the SOS's anchor (or its nearest ancestor that has a rota). A city with no rota, or a
role left empty, is recorded as such and the chain moves on, so the gap is visible, never silent.
On QA the call bridge is the fake (CALL_BRIDGE_PROVIDER=fake) and SMS is the sink.
"""
import logging
from datetime import timedelta

from django.db import IntegrityError, transaction

from servers.admin_audit.services import record_admin_action

from .models import U_PAGED, OnCallRota, SOSPage

logger = logging.getLogger(__name__)

CHAIN = OnCallRota.ROLES


def rota_for(node):
    """The rota of `node` or of its nearest ancestor that has one."""
    seen = 0
    while node is not None and seen < 8:
        rota = OnCallRota.objects.filter(city=node).first()
        if rota is not None:
            return rota
        node, seen = node.parent, seen + 1
    return None


def due_step(event, minutes, now):
    """How many pages should have gone out by `now`: one per interval after the first."""
    if not minutes:
        return 0
    elapsed = (now - event.created_at).total_seconds()
    return int(elapsed // (minutes * 60))


def target_of(step):
    return CHAIN[(step - 1) % len(CHAIN)]


def _call(phone, message):
    from servers.callbridge.providers import CallingUnavailable, country_of_phone, get_provider

    try:
        provider = get_provider(country_of_phone(phone))
        return provider.name, provider.page(phone, message, 'sos_escalation')
    except CallingUnavailable:
        return '', None
    except Exception:  # noqa: BLE001 - a failed call falls back to SMS
        logger.exception('oncall: call failed')
        return '', None


def _sms(phone, text, ref_name=''):
    from servers.sharing.sms import mode

    if mode() != 'live':
        return 'sms-sink'
    try:
        from base.utils import send_otp_via_sns, stash_sms_message

        send_otp_via_sns.delay(phone, stash_sms_message(
            text, ttl=3600, kind='oncall_page' if ref_name else '', values={'ref': ref_name}))
        return 'sms'
    except Exception:  # noqa: BLE001
        logger.exception('oncall: sms failed')
        return None


def page(event, step, now):
    """Send page `step` for `event`, once (a second run loses the unique row and does nothing)."""
    from base import refs

    role = target_of(step)
    rota = rota_for(event.anchor_node)
    name, phone = rota.contact(role) if rota else ('', '')
    ref_name = refs.incident_ref(event.incident) if event.incident_id else f'SOS-{event.id}'
    from base import sms_templates
    message = sms_templates.render('oncall_page', ref=ref_name)
    channel, outcome, provider, ref = 'none', 'no_rota' if rota is None else 'no_number', '', ''
    if phone:
        provider, ref = _call(phone, message)
        if ref:
            channel, outcome = 'call', 'placed'
        else:
            sent = _sms(phone, message, ref_name)
            channel, outcome, provider = 'sms', ('sent' if sent else 'failed'), sent or ''
    try:
        with transaction.atomic():
            row = SOSPage.objects.create(sos=event, step=step, target=role, name=name[:120],
                                         phone_last4=phone[-4:] if phone else '', channel=channel,
                                         outcome=outcome, provider=provider[:32], ref=(ref or '')[:64])
            who = f'{role} ({name})' if name else role
            how = {'call': 'phoned', 'sms': 'sent an SMS to', 'none': 'could not page'}[channel]
            note = (f'Escalation {step}: {how} the on-call {who}' + (f' ending {phone[-4:]}' if phone else '')
                    + ('' if outcome in ('placed', 'sent') else f' ({outcome.replace("_", " ")})'))
            if event.incident_id:
                from . import services
                services.add_update(event.incident, U_PAGED, label=services.SYSTEM, note=note,
                                    data={'sos_id': event.id, 'step': step, 'target': role,
                                          'channel': channel, 'outcome': outcome})
            from servers.sos.models import SOSEventUpdate
            SOSEventUpdate.objects.create(event=event, actor_label='System', note=note)
            record_admin_action(
                None, action='safety_sos_paged', target_type='sos_event', target_id=event.id,
                before={'step': step - 1}, after={'step': step, 'target': role, 'channel': channel,
                                                  'outcome': outcome, 'phone_last4': row.phone_last4},
                reason=note)
    except IntegrityError:
        return None
    return row


def sweep(events_and_minutes, now):
    """For each (unacknowledged SOS, its interval): send the next page if one is due. One page per
    SOS per run, so a sweep that was down catches up one interval at a time."""
    sent = 0
    for event, minutes in events_and_minutes:
        due = due_step(event, minutes, now)
        if due < 1:
            continue
        last = SOSPage.objects.filter(sos=event).order_by('-step').values_list('step', flat=True).first() or 0
        if last >= due:
            continue
        if page(event, last + 1, now) is not None:
            sent += 1
    return sent


def next_page_at(event, minutes):
    """When the next page goes out, for the console; None once acknowledged."""
    if event.status != 'open' or not minutes:
        return None
    last = SOSPage.objects.filter(sos=event).order_by('-step').values_list('step', flat=True).first() or 0
    return event.created_at + timedelta(minutes=minutes * (last + 1))
