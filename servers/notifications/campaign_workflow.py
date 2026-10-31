"""Campaigns: promotional broadcasts, sent only after a checker approves.

    draft -> submit -> pending_approval -> (checker approves) -> scheduled -> sending -> sent
                |                                  |
                +--- cancel (draft / pending / scheduled) -> cancelled

SENDING IS A BULK ACTION: `notification.campaign_send` always needs a checker, in India and the
US. A rejected, cancelled or expired request returns the campaign to `draft`. Cancel is
allowed until sending starts.

THE AUDIENCE
------------
role (drivers | riders), the cities in scope, and filters (driver status, last-active window).
The count preview shows the whole funnel, because "how many people will this reach?" is the
checker's question:

    total -> not opted in (promotional is OFF until a person opts in) -> no contact on file
          -> reachable -> of which held by quiet hours (they send at the window's end)

Delivery goes through the same dispatcher (`dispatcher.enqueue`), so preferences, quiet hours,
the allowlist and the stub apply exactly as for a system message. Expanding the audience is
idempotent per (campaign, user), so a resumed run never double-sends.
"""

import hashlib
import json
import logging
from collections import Counter
from datetime import timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Count, Exists, OuterRef, Q
from django.utils import timezone

from base.countries import COUNTRIES
from servers.admin_access.bridge import granted
from servers.admin_access.models import CITY, GeoNode
from servers.admin_access.scoping import scoped, scoped_or_none
from servers.admin_audit.services import record_admin_action
from servers.approvals import registry
from servers.approvals.actions import NOTIFICATION_CAMPAIGN_SEND
from servers.approvals.service import (
    InvalidRequest, NotInScope, NotPermitted, WrongState, require_approval_or_execute,
)

from . import config, dispatcher, quiet
from .models import (
    ACTIVE, CAMPAIGN_CANCELLED, CAMPAIGN_DRAFT, CAMPAIGN_PENDING, CAMPAIGN_SCHEDULED,
    CAMPAIGN_SENDING, CAMPAIGN_SENT, EMAIL, PROMOTIONAL, PUSH, SUPPRESSED,
    Campaign, NotificationMessage, NotificationPreference, NotificationTemplate,
)

logger = logging.getLogger(__name__)

CAPABILITY = 'notify.campaign'
ROLES = ('drivers', 'riders')
DRIVER_STATUSES = ('online', 'offline', 'busy')
STUCK_MINUTES = 10


# ---------------------------------------------------------------------------
# Audience
# ---------------------------------------------------------------------------

def lowest_common_ancestor(nodes):
    chains = []
    for node in nodes:
        chain, cur = [], node
        while cur is not None:
            chain.append(cur)
            cur = cur.parent
        chains.append(list(reversed(chain)))
    common = None
    for level in zip(*chains, strict=False):
        if len({n.pk for n in level}) == 1:
            common = level[0]
        else:
            break
    return common


def clean_audience(actor, raw):
    """`(audience, city_nodes, anchor)`. City codes outside the caller's scope are a 404."""
    raw = raw or {}
    role = raw.get('role')
    if role not in ROLES:
        raise InvalidRequest('audience.role must be drivers or riders.', code='INVALID_FIELD',
                             field='audience')
    codes = raw.get('cities')
    if not isinstance(codes, list) or not codes or not all(isinstance(c, str) for c in codes):
        raise InvalidRequest('Name at least one city.', code='INVALID_FIELD', field='audience')
    nodes = []
    for code in sorted(set(codes)):
        node = scoped_or_none(actor, GeoNode.objects.filter(kind=CITY), code=code)
        if node is None or not granted(actor, CAPABILITY, node)[0]:
            raise NotInScope(f'No city {code!r} in your scope.', field='audience')
        nodes.append(node)
    countries = {n.country.code for n in nodes if n.country is not None}
    if len(countries) != 1:
        raise InvalidRequest('A campaign targets the cities of one country.',
                             code='INVALID_FIELD', field='audience')
    anchor = lowest_common_ancestor(nodes)
    if anchor is None or not granted(actor, CAPABILITY, anchor)[0]:
        raise NotPermitted('A campaign spanning several cities needs a scope covering all of '
                           'them.', code='SCOPE_NOT_HELD', field='audience')
    statuses = raw.get('status') or []
    if role == 'drivers' and (not isinstance(statuses, list)
                              or any(s not in DRIVER_STATUSES for s in statuses)):
        raise InvalidRequest('audience.status must be a list of: ' + ', '.join(DRIVER_STATUSES),
                             code='INVALID_FIELD', field='audience')
    days = raw.get('last_active_days')
    if days is not None:
        if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 365:
            raise InvalidRequest('audience.last_active_days must be 1 to 365.',
                                 code='INVALID_FIELD', field='audience')
    audience = {'role': role, 'cities': [n.code for n in nodes],
                'status': statuses if role == 'drivers' else [], 'last_active_days': days}
    return audience, nodes, anchor


def audience_users(audience):
    """Active accounts in the audience (before opt-in and contact checks)."""
    from servers.ride.models import Trip
    from servers.rider.scope import trip_window_start

    User = get_user_model()
    nodes = list(GeoNode.objects.filter(kind=CITY, code__in=audience.get('cities') or []))
    if not nodes:
        return User.objects.none()
    if audience.get('role') == 'drivers':
        in_city = Q(pk__in=[])
        for node in nodes:
            in_city |= Q(driver__home_city__path__startswith=node.path)
        users = User.objects.filter(role='driver', is_active=True, driver__approved=True).filter(in_city)
        if audience.get('status'):
            users = users.filter(driver__status__in=audience['status'])
    else:
        home = Q(pk__in=[])
        trip_city = Q(pk__in=[])
        for node in nodes:
            home |= Q(rider__home_city__path__startswith=node.path)
            trip_city |= Q(zone__geo_node__path__startswith=node.path)
        recent = Trip.objects.filter(user_id=OuterRef('pk'), requested_at__gte=trip_window_start())
        users = User.objects.filter(role='rider', is_active=True, rider__isnull=False).filter(
            home | Exists(recent.filter(trip_city)))
    if audience.get('last_active_days'):
        users = users.filter(last_login__gte=timezone.now()
                             - timedelta(days=audience['last_active_days']))
    return users.distinct()


def _contactable(users, channel):
    if channel == EMAIL:
        return users.exclude(Q(email__isnull=True) | Q(email=''))
    if channel == PUSH:
        return users.exclude(Q(fcm_token__isnull=True) | Q(fcm_token=''))
    return users


def _zone_for(node_id, country):
    from servers.pricing.models import ServiceZone

    if node_id:
        zone = (ServiceZone.objects.filter(geo_node_id=node_id, is_active=True)
                .exclude(timezone_name='').first())
        if zone is not None:
            try:
                return ZoneInfo(zone.timezone_name)
            except ZoneInfoNotFoundError:
                pass
    market = COUNTRIES.get(country)
    return ZoneInfo(market.default_timezone if market else 'UTC')


def preview(audience, template, *, now=None):
    """The audience funnel for `template`'s channel. No messages are created."""
    now = now or timezone.now()
    users = audience_users(audience)
    total = users.count()
    contactable = _contactable(users, template.channel)
    unreachable_on_channel = total - contactable.count()
    opted = NotificationPreference.objects.filter(
        user_id=OuterRef('pk'), channel=template.channel, category=PROMOTIONAL, opted_in=True)
    reachable_qs = contactable.filter(Exists(opted))
    reachable = reachable_qs.count()
    not_opted_in = contactable.count() - reachable

    # Quiet hours: group by the recipient's home city (driver or rider), then local time.
    held, release_at = 0, None
    groups = (reachable_qs.order_by().values('driver__home_city_id', 'rider__home_city_id')
              .annotate(n=Count('pk', distinct=True)))
    for group in groups:
        driver_city, rider_city = group['driver__home_city_id'], group['rider__home_city_id']
        count = group['n']
        tz = _zone_for(driver_city or rider_city, template.country)
        release = quiet.release_time(now, tz, template.country)
        if release is not None:
            held += count
            release_at = release if release_at is None else min(release_at, release)
    return {
        'total': total, 'not_opted_in': not_opted_in, 'unreachable_on_channel': unreachable_on_channel,
        'reachable': reachable, 'held_by_quiet_hours': held,
        'send_now': reachable - held,
        'quiet_hours': list(config.quiet_hours(template.country)),
        'earliest_release_at': release_at.isoformat() if release_at else None,
        'channel': template.channel,
    }


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------

def campaign_dict(c):
    return {
        'id': c.id, 'name': c.name, 'country': c.country, 'status': c.status,
        'template': {'id': c.template_id, 'key': c.template.key, 'channel': c.template.channel,
                     'version': c.template.version, 'status': c.template.status},
        'audience': c.audience, 'template_values': c.template_values,
        'scope': c.geo_node.code if c.geo_node_id else None,
        'scheduled_at': c.scheduled_at.isoformat() if c.scheduled_at else None,
        'audience_count': c.audience_count, 'queued_count': c.queued_count,
        'suppressed_count': c.suppressed_count, 'reason': c.reason,
        'approval_request_id': c.approval_request_id,
        'created_by': {'id': c.created_by_id, 'email': c.created_by_email,
                       'label': c.created_by_label, 'role': c.created_by_role},
        'created_at': c.created_at.isoformat(),
        'started_at': c.started_at.isoformat() if c.started_at else None,
        'finished_at': c.finished_at.isoformat() if c.finished_at else None,
        'cancelled_at': c.cancelled_at.isoformat() if c.cancelled_at else None,
    }


# ---------------------------------------------------------------------------
# Drafting
# ---------------------------------------------------------------------------

def _template_for(actor, template_id):
    template = scoped_or_none(actor, NotificationTemplate.objects.select_related('geo_node'),
                              pk=template_id)
    if template is None:
        raise NotInScope('No such template in your scope.', field='template')
    if template.status != ACTIVE or template.category != PROMOTIONAL:
        raise InvalidRequest('A campaign needs an ACTIVE promotional template.',
                             code='TEMPLATE_NOT_USABLE', field='template')
    if template.personal_variables:
        raise InvalidRequest('A campaign template cannot use personal variables.',
                             code='TEMPLATE_NOT_USABLE', field='template')
    return template


def _clean_values(template, values):
    if values is None:
        return {}
    if not isinstance(values, dict):
        raise InvalidRequest('template_values must be an object.', code='INVALID_FIELD',
                             field='template_values')
    unknown = sorted(set(values) - set(template.variables or []))
    if unknown:
        raise InvalidRequest(f'Unknown template variables: {", ".join(unknown)}.',
                             code='INVALID_FIELD', field='template_values')
    return {k: str(v)[:300] for k, v in values.items()}


def _clean_schedule(value):
    if value in (None, ''):
        return None
    from django.utils.dateparse import parse_datetime

    parsed = parse_datetime(str(value))
    if parsed is None:
        raise InvalidRequest('scheduled_at must be an ISO 8601 date and time.',
                             code='INVALID_FIELD', field='scheduled_at')
    if timezone.is_naive(parsed):
        raise InvalidRequest('scheduled_at needs a UTC offset.', code='INVALID_FIELD',
                             field='scheduled_at')
    return parsed


def _gate(user, campaign):
    if campaign.geo_node is None or not granted(user, CAPABILITY, campaign.geo_node)[0]:
        raise NotPermitted('You do not hold notify.campaign here.', code='SCOPE_NOT_HELD',
                           field='campaign')


@transaction.atomic
def create_draft(actor, data, *, http_request=None):
    name = str(data.get('name') or '').strip()
    if not name:
        raise InvalidRequest('A campaign needs a name.', code='INVALID_FIELD', field='name')
    template = _template_for(actor, data.get('template_id'))
    audience, nodes, anchor = clean_audience(actor, data.get('audience'))
    country = anchor.country.code if anchor.country is not None else ''
    if template.country != country:
        raise InvalidRequest(f'The template is for {template.country or "no country"}; the '
                             f'audience is in {country}.', code='COUNTRY_MISMATCH',
                             field='template')
    campaign = Campaign(
        name=name[:120], country=country, template=template, audience=audience,
        template_values=_clean_values(template, data.get('template_values')),
        scheduled_at=_clean_schedule(data.get('scheduled_at')), geo_node=anchor,
        reason=str(data.get('reason') or '')[:2000])
    campaign.set_created_by(actor)
    campaign.save()
    record_admin_action(http_request, action='notification_campaign_created',
                        target_type='notification_campaign', target_id=campaign.id,
                        after=campaign_dict(campaign), reason=campaign.reason)
    return campaign


@transaction.atomic
def update_draft(actor, campaign, data, *, http_request=None):
    _gate(actor, campaign)
    campaign = Campaign.objects.select_for_update().get(pk=campaign.pk)
    if campaign.status != CAMPAIGN_DRAFT:
        raise WrongState('Only a draft campaign can be edited.', code='CAMPAIGN_NOT_DRAFT',
                         field='status')
    before = campaign_dict(campaign)
    if 'name' in data:
        campaign.name = str(data['name']).strip()[:120] or campaign.name
    if 'template_id' in data:
        campaign.template = _template_for(actor, data['template_id'])
    if 'audience' in data:
        campaign.audience, _nodes, anchor = clean_audience(actor, data['audience'])
        campaign.geo_node = anchor
        campaign.country = anchor.country.code if anchor.country is not None else ''
    if campaign.template.country != campaign.country:
        raise InvalidRequest('The template and the audience must be in the same country.',
                             code='COUNTRY_MISMATCH', field='template')
    if 'template_values' in data or 'template_id' in data:
        campaign.template_values = _clean_values(
            campaign.template, data.get('template_values', campaign.template_values))
    if 'scheduled_at' in data:
        campaign.scheduled_at = _clean_schedule(data['scheduled_at'])
    if 'reason' in data:
        campaign.reason = str(data['reason'])[:2000]
    campaign.save()
    record_admin_action(http_request, action='notification_campaign_updated',
                        target_type='notification_campaign', target_id=campaign.id,
                        before=before, after=campaign_dict(campaign), reason=campaign.reason)
    return campaign


# ---------------------------------------------------------------------------
# Submit, cancel
# ---------------------------------------------------------------------------

def _digest(payload):
    raw = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:24]
    return '-'.join(raw[i:i + 6] for i in range(0, 24, 6))


def propose_send(actor, campaign, *, reason='', http_request=None):
    _gate(actor, campaign)
    reason = (reason or campaign.reason or '').strip()
    if not reason:
        raise InvalidRequest('A reason is required.', code='REASON_REQUIRED', field='reason')
    with transaction.atomic():
        locked = (Campaign.objects.select_for_update().select_related('template', 'geo_node')
                  .get(pk=campaign.pk))
        if locked.status != CAMPAIGN_DRAFT:
            raise WrongState('Only a draft campaign can be submitted.', code='CAMPAIGN_NOT_DRAFT',
                             field='status')
        template = locked.template
        if template.status != ACTIVE:
            raise InvalidRequest('The template is no longer active.', code='TEMPLATE_NOT_USABLE',
                                 field='template')
        if locked.scheduled_at is not None and locked.scheduled_at < timezone.now() - timedelta(minutes=1):
            raise InvalidRequest('The scheduled time has passed.', code='SCHEDULE_IN_PAST',
                                 field='scheduled_at')
        funnel = preview(locked.audience, template)
        if funnel['reachable'] == 0:
            raise InvalidRequest(
                'Nobody in this audience can receive it: promotional messages go only to people '
                'who opted in.', code='EMPTY_AUDIENCE', field='audience')
        payload = {
            'campaign_id': locked.id, 'name': locked.name, 'template_key': template.key,
            'template_version': template.version, 'channel': template.channel,
            'audience': locked.audience, 'funnel': funnel,
            'scheduled_at': locked.scheduled_at.isoformat() if locked.scheduled_at else None}
        outcome = require_approval_or_execute(
            NOTIFICATION_CAMPAIGN_SEND, actor=actor, country=locked.country,
            scope_node=locked.geo_node, target_type='notification_campaign',
            target_id=str(locked.id), payload=payload,
            idempotency_key=f'notification.campaign_send:{locked.id}:{_digest(payload)}',
            reason=reason, http_request=http_request)
        locked.status = CAMPAIGN_PENDING
        locked.audience_count = funnel['reachable']
        locked.approval_request_id = outcome.request.pk
        locked.save(update_fields=['status', 'audience_count', 'approval_request_id', 'updated_at'])
    record_admin_action(http_request, action='notification_campaign_submitted',
                        target_type='notification_campaign', target_id=locked.id,
                        after=campaign_dict(locked), reason=reason)
    return outcome


def cancel(actor, campaign, *, reason='', http_request=None):
    _gate(actor, campaign)
    from servers.approvals.models import REQUESTED, ApprovalRequest
    from servers.approvals import service as approvals

    with transaction.atomic():
        locked = Campaign.objects.select_for_update().get(pk=campaign.pk)
        if locked.status not in (CAMPAIGN_DRAFT, CAMPAIGN_PENDING, CAMPAIGN_SCHEDULED):
            raise WrongState('A campaign can be cancelled until it starts sending.',
                             code='CAMPAIGN_NOT_CANCELLABLE', field='status')
        before = campaign_dict(locked)
        pending = None
        if locked.status == CAMPAIGN_PENDING and locked.approval_request_id:
            pending = ApprovalRequest.objects.filter(
                pk=locked.approval_request_id, status=REQUESTED).first()
        locked.status = CAMPAIGN_CANCELLED
        locked.cancelled_at = timezone.now()
        locked.save(update_fields=['status', 'cancelled_at', 'updated_at'])
        record_admin_action(http_request, action='notification_campaign_cancelled',
                            target_type='notification_campaign', target_id=locked.id,
                            before=before, after=campaign_dict(locked), reason=reason)
    if pending is not None and pending.maker_id == getattr(actor, 'pk', None):
        try:
            approvals.cancel(pending, actor, reason or 'campaign cancelled', http_request)
        except approvals.ApprovalError:
            logger.warning('campaign %s cancelled but its approval request stays open',
                           locked.id)
    return locked


# ---------------------------------------------------------------------------
# The checker's side, and the send
# ---------------------------------------------------------------------------

@registry.register(NOTIFICATION_CAMPAIGN_SEND)
def execute_campaign_send(request):
    with transaction.atomic():
        campaign = (Campaign.objects.select_for_update().select_related('template')
                    .filter(pk=(request.payload or {}).get('campaign_id')).first())
        if campaign is None:
            raise registry.ExecutionRefused('CAMPAIGN_GONE', 'The campaign no longer exists.')
        if campaign.status != CAMPAIGN_PENDING:
            raise registry.ExecutionRefused('CAMPAIGN_NOT_PENDING',
                                            'The campaign is no longer waiting for approval.')
        template = campaign.template
        if template.status != ACTIVE or template.version != request.payload.get('template_version'):
            raise registry.ExecutionRefused(
                'TEMPLATE_CHANGED', 'The template changed after the campaign was submitted.')
        campaign.status = CAMPAIGN_SCHEDULED
        if campaign.scheduled_at is None:
            campaign.scheduled_at = timezone.now()
        campaign.save(update_fields=['status', 'scheduled_at', 'updated_at'])
        record_admin_action(
            None, action='notification_campaign_approved', target_type='notification_campaign',
            target_id=campaign.id, after=campaign_dict(campaign),
            reason=request.decision_reason or request.request_reason)
        campaign_id = campaign.id
    transaction.on_commit(lambda: _schedule_run(campaign_id))
    return {'campaign_id': campaign_id, 'status': CAMPAIGN_SCHEDULED}


def _schedule_run(campaign_id):
    try:
        from .tasks import run_campaign
        run_campaign.delay(campaign_id)
    except Exception:  # noqa: BLE001 - the beat sweep starts due campaigns
        logger.exception('campaign_schedule_failed campaign_id=%s', campaign_id)


@registry.on_closed(NOTIFICATION_CAMPAIGN_SEND)
def campaign_request_closed(request, outcome):
    campaign = Campaign.objects.select_for_update().filter(
        pk=(request.payload or {}).get('campaign_id')).first()
    if campaign is not None and campaign.status == CAMPAIGN_PENDING:
        campaign.status = CAMPAIGN_DRAFT
        campaign.save(update_fields=['status', 'updated_at'])


def start(campaign_id, now=None):
    """Expand a scheduled campaign into outbox rows. Safe to call again after a crash."""
    now = now or timezone.now()
    claimed = (Campaign.objects.filter(pk=campaign_id, status=CAMPAIGN_SCHEDULED)
               .filter(Q(scheduled_at__isnull=True) | Q(scheduled_at__lte=now))
               .update(status=CAMPAIGN_SENDING, started_at=now))
    campaign = Campaign.objects.select_related('template').get(pk=campaign_id)
    if not claimed and campaign.status != CAMPAIGN_SENDING:
        return 'skipped'
    template = campaign.template
    users = _contactable(audience_users(campaign.audience), template.channel).filter(
        Exists(NotificationPreference.objects.filter(
            user_id=OuterRef('pk'), channel=template.channel, category=PROMOTIONAL,
            opted_in=True)))
    for user in users.select_related().iterator(chunk_size=500):
        dispatcher.enqueue(user, template, campaign.template_values,
                           idempotency_key=f'campaign:{campaign.pk}:user:{user.pk}',
                           source=f'campaign:{campaign.pk}', campaign=campaign, now=now)
    counts = Counter(NotificationMessage.objects.filter(campaign=campaign)
                     .values_list('status', flat=True))
    Campaign.objects.filter(pk=campaign.pk).update(
        status=CAMPAIGN_SENT, finished_at=timezone.now(),
        queued_count=sum(n for s, n in counts.items() if s != SUPPRESSED),
        suppressed_count=counts.get(SUPPRESSED, 0))
    record_admin_action(
        None, action='notification_campaign_sent', target_type='notification_campaign',
        target_id=campaign.pk, after={'counts': dict(counts)}, reason='campaign expanded')
    return 'sent'


def due_ids(now=None):
    now = now or timezone.now()
    due = Campaign.objects.filter(status=CAMPAIGN_SCHEDULED).filter(
        Q(scheduled_at__isnull=True) | Q(scheduled_at__lte=now)).values_list('pk', flat=True)
    stuck = Campaign.objects.filter(
        status=CAMPAIGN_SENDING, started_at__lt=now - timedelta(minutes=STUCK_MINUTES)
    ).values_list('pk', flat=True)
    return list(due) + list(stuck)


def visible(user):
    return scoped(user, Campaign.objects.select_related('template', 'geo_node'))
