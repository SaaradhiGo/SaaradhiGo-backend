"""Campaign life cycle: draft, submit, approve, pause, resume, cancel, raise the budget.

    draft -> submit -> pending_approval -> (a second admin approves) -> scheduled | live
       |                     |                                              |
       |                     +-- rejected / cancelled / expired -> draft    +-- pause <-> resume
       +--------------- cancel (any state before ended) -> cancelled         +-- budget used -> exhausted
                                                                            +-- window passes -> ended

TWO PEOPLE BEFORE MONEY IS COMMITTED (owner ruling, AUDIT 27.51)
----------------------------------------------------------------
* `promotion.launch`: ANY campaign going live, of any kind, in India and the United States,
  needs a checker (`approval.check`, a different admin). The checker approves the exact terms
  the maker submitted: the executor runs those terms and nothing else.
* `promotion.budget_increase`: a budget can only go UP, and only through a checker.
* Pausing and cancelling need no checker (they only reduce spend) and are audited. Resuming a
  paused campaign puts the SAME approved terms back in force, so it needs none either.
  A cancelled campaign still honours every trip that already holds a discount.

WHO MAY WRITE WHAT
------------------
`promo.write` (Marketing): rider promos and rider referrals. `incentive.write` (City Ops
Manager): driver incentives and driver referrals. Super Admin holds both. The capability is
checked AT THE CAMPAIGN'S PLACE; a place outside the caller's scope is a 404.

THE PLACE
---------
A campaign is anchored at a country or a city node. A city campaign serves that city's zones,
trips and drivers; a country campaign the whole country.

WRAPPED LEGACY ROWS
-------------------
Campaigns created from existing `PromoCode` / `IncentiveQuest` rows (`is_legacy`) have no
budget and no checker; they keep behaving as they did. They can be paused or cancelled here (which
flips the old row's `is_active`) but not edited or re-budgeted: launch a new campaign instead.
"""

import hashlib
import json
import logging
import re
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.db import IntegrityError, transaction
from django.db.models import Avg
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from base.countries import get_country
from servers.admin_access.bridge import ScopedPermission, granted
from servers.admin_access.models import CITY, COUNTRY, GeoNode
from servers.admin_access.scoping import scoped, scoped_or_none
from servers.admin_audit.services import record_admin_action
from servers.approvals import registry
from servers.approvals.actions import PROMOTION_BUDGET_INCREASE, PROMOTION_LAUNCH
from servers.approvals.service import (
    InvalidRequest, NotInScope, NotPermitted, WrongState, require_approval_or_execute,
)

from .models import (
    CANCELLED, DRAFT, DRIVER_INCENTIVE, ENDED, EXHAUSTED, KINDS, LIVE, PAUSED, PENDING,
    REFERRAL, RIDER_PROMO, SCHEDULED, Campaign,
)

logger = logging.getLogger(__name__)

PROMO_WRITE, INCENTIVE_WRITE, PROMO_READ = 'promo.write', 'incentive.write', 'promo.read'
CODE_RE = re.compile(r'^[A-Z0-9][A-Z0-9_-]{2,31}$')
MAX_BUDGET_MINOR = 10 ** 12


class PromoWrite(ScopedPermission):
    """The gate on every write route: the caller must hold `promo.write` OR `incentive.write`
    somewhere. WHICH one decides, and WHERE, is checked per campaign by `require_write`."""

    def __init__(self):
        super().__init__(PROMO_WRITE)

    def has_permission(self, request, view):
        return (granted(request.user, PROMO_WRITE)[0]
                or granted(request.user, INCENTIVE_WRITE)[0])


def write_capability(kind, config=None):
    """The capability that owns a kind: rider promos and rider referrals are Marketing's
    (`promo.write`); driver incentives and driver referrals are City Ops' (`incentive.write`)."""
    if kind == RIDER_PROMO:
        return PROMO_WRITE
    if kind == DRIVER_INCENTIVE:
        return INCENTIVE_WRITE
    return PROMO_WRITE if (config or {}).get('audience') == 'rider' else INCENTIVE_WRITE


def require_write(actor, campaign):
    capability = write_capability(campaign.kind, campaign.config)
    if campaign.geo_node is None or not granted(actor, capability, campaign.geo_node)[0]:
        raise NotPermitted(f'You do not hold {capability} here.', code='SCOPE_NOT_HELD',
                           field='campaign')
    return capability


# ---------------------------------------------------------------------------
# Input cleaning
# ---------------------------------------------------------------------------

def _int(value, field, *, minimum=0, maximum=MAX_BUDGET_MINOR, required=True):
    if value is None or value == '':
        if required:
            raise InvalidRequest(f'{field} is required.', code='INVALID_FIELD', field=field)
        return None
    if isinstance(value, bool) or isinstance(value, float) or (
            isinstance(value, str) and not re.fullmatch(r'-?\d+', value.strip())):
        raise InvalidRequest(f'{field} must be a whole number.', code='INVALID_FIELD', field=field)
    number = int(value)
    if number < minimum or number > maximum:
        # No bounds in the text: a huge bound reads as a long digit run to the PII sweep.
        raise InvalidRequest(f'{field} is out of range (at least {minimum}).',
                             code='INVALID_FIELD', field=field)
    return number


def _datetime(value, field):
    parsed = parse_datetime(str(value or ''))
    if parsed is None:
        raise InvalidRequest(f'{field} must be an ISO 8601 date and time.', code='INVALID_FIELD',
                             field=field)
    if timezone.is_naive(parsed):
        raise InvalidRequest(f'{field} needs a UTC offset.', code='INVALID_FIELD', field=field)
    return parsed


def resolve_node(actor, code, capability):
    """The country or city node named `code`, in the caller's scope, with `capability` held."""
    node = scoped_or_none(actor, GeoNode.objects.filter(kind__in=(COUNTRY, CITY)).select_related(
        'parent'), code=str(code or ''))
    if node is None:
        raise NotInScope(f'No place {code!r} in your scope.', field='scope')
    if not granted(actor, capability, node)[0]:
        raise NotPermitted(f'You do not hold {capability} at {node.code}.',
                           code='SCOPE_NOT_HELD', field='scope')
    return node


def country_of_node(node):
    if node.kind == COUNTRY:
        return node.code
    return node.country.code if node.country is not None else ''


def _zone(code, node, field='zone_code'):
    from servers.pricing.models import ServiceZone

    if not code:
        return None
    zone = ServiceZone.objects.filter(code=code).select_related('geo_node').first()
    if zone is None or zone.geo_node is None or not str(zone.geo_node.path).startswith(node.path):
        raise InvalidRequest(f'Zone {code!r} is not inside {node.code}.', code='INVALID_FIELD',
                             field=field)
    return zone


def clean_config(kind, raw, node):
    """The kind's settings, validated. Money is integer minor units of the node's currency."""
    if not isinstance(raw, dict):
        raise InvalidRequest('config must be an object.', code='INVALID_FIELD', field='config')
    country = country_of_node(node)
    currency = get_country(country).currency
    if kind == RIDER_PROMO:
        return _clean_promo(raw, node, currency)
    if kind == DRIVER_INCENTIVE:
        return _clean_incentive(raw, node)
    return _clean_referral(raw, currency)


def _clean_promo(raw, node, currency):
    auto = raw.get('auto_apply') is True
    code = str(raw.get('code') or '').strip().upper()
    if not auto and not code:
        raise InvalidRequest('A promo needs a code, or must be auto-applied.',
                             code='INVALID_FIELD', field='code')
    if code and not CODE_RE.match(code):
        raise InvalidRequest('A code is 3 to 32 letters, digits, dashes or underscores.',
                             code='INVALID_FIELD', field='code')
    kind = raw.get('discount_type')
    if kind not in ('percent', 'flat'):
        raise InvalidRequest('discount_type must be percent or flat.', code='INVALID_FIELD',
                             field='discount_type')
    cfg = {'code': code, 'auto_apply': auto, 'discount_type': kind,
           'discount_percent': None, 'discount_flat_minor': None, 'max_discount_minor': None}
    if kind == 'percent':
        try:
            pct = Decimal(str(raw.get('discount_percent')))
        except (InvalidOperation, ValueError, TypeError):
            raise InvalidRequest('discount_percent must be a number.', code='INVALID_FIELD',
                                 field='discount_percent')
        if not pct.is_finite() or not (Decimal('0') < pct <= Decimal('100')):
            raise InvalidRequest('discount_percent must be above 0 and at most 100.',
                                 code='INVALID_FIELD', field='discount_percent')
        cfg['discount_percent'] = str(pct.quantize(Decimal('0.01')))
        cfg['max_discount_minor'] = _int(raw.get('max_discount_minor'), 'max_discount_minor',
                                         minimum=1, required=False)
    else:
        cfg['discount_flat_minor'] = _int(raw.get('discount_flat_minor'), 'discount_flat_minor',
                                          minimum=1)
    cfg['min_fare_minor'] = _int(raw.get('min_fare_minor') or 0, 'min_fare_minor')
    cfg['max_per_user'] = _int(raw.get('max_per_user') or 1, 'max_per_user', minimum=1,
                               maximum=1000)
    cfg['max_total_redemptions'] = _int(raw.get('max_total_redemptions'), 'max_total_redemptions',
                                        minimum=1, required=False)
    zone_code = str(raw.get('zone_code') or '').strip()
    _zone(zone_code, node)
    cfg['zone_code'] = zone_code
    cfg['new_riders_only'] = raw.get('new_riders_only') is True
    return cfg


def _clean_incentive(raw, node):
    from servers.driver.models import VehicleType

    cfg = {'target_trips': _int(raw.get('target_trips'), 'target_trips', minimum=1, maximum=1000),
           'reward_minor': _int(raw.get('reward_minor'), 'reward_minor', minimum=1)}
    zone_code = str(raw.get('zone_code') or '').strip()
    _zone(zone_code, node)
    cfg['zone_code'] = zone_code
    hour_from, hour_to = raw.get('hour_from'), raw.get('hour_to')
    if (hour_from in (None, '')) != (hour_to in (None, '')):
        raise InvalidRequest('Give both hour_from and hour_to, or neither.',
                             code='INVALID_FIELD', field='hour_from')
    if hour_from in (None, ''):
        cfg['hour_from'] = cfg['hour_to'] = None
    else:
        cfg['hour_from'] = _int(hour_from, 'hour_from', minimum=0, maximum=23)
        cfg['hour_to'] = _int(hour_to, 'hour_to', minimum=0, maximum=24)
        if cfg['hour_from'] == cfg['hour_to']:
            raise InvalidRequest('The hours must differ.', code='INVALID_FIELD', field='hour_to')
    vehicle = str(raw.get('vehicle_type') or '').strip()
    if vehicle and not VehicleType.objects.filter(type__iexact=vehicle).exists():
        raise InvalidRequest(f'No vehicle class {vehicle!r}.', code='INVALID_FIELD',
                             field='vehicle_type')
    cfg['vehicle_type'] = vehicle
    return cfg


def _clean_referral(raw, currency):
    from django.conf import settings

    audience = raw.get('audience')
    if audience not in ('rider', 'driver'):
        raise InvalidRequest('audience must be rider or driver.', code='INVALID_FIELD',
                             field='audience')
    cfg = {'audience': audience,
           'required_trips': _int(raw.get('required_trips'), 'required_trips', minimum=1,
                                  maximum=100),
           'referrer_reward_minor': _int(raw.get('referrer_reward_minor') or 0,
                                         'referrer_reward_minor'),
           'referee_reward_minor': _int(raw.get('referee_reward_minor') or 0,
                                        'referee_reward_minor')}
    if cfg['referrer_reward_minor'] + cfg['referee_reward_minor'] <= 0:
        raise InvalidRequest('Reward the referrer, the referee, or both.', code='INVALID_FIELD',
                             field='referrer_reward_minor')
    if audience == 'rider':
        cap = int(Decimal(str(getattr(settings, 'RIDER_CREDIT_BALANCE_CAP', '2000.00')))
                  * (10 ** 2))
        if max(cfg['referrer_reward_minor'], cfg['referee_reward_minor']) > cap:
            raise InvalidRequest('A rider reward cannot exceed the wallet balance cap.',
                                 code='INVALID_FIELD', field='referrer_reward_minor')
    return cfg


def clean_budget(value, kind, config):
    budget = _int(value, 'budget_minor', minimum=1)
    floor = {RIDER_PROMO: 1,
             DRIVER_INCENTIVE: int(config.get('reward_minor') or 1),
             REFERRAL: int(config.get('referrer_reward_minor') or 0)
             + int(config.get('referee_reward_minor') or 0)}[kind]
    if budget < floor:
        raise InvalidRequest('The budget cannot fund even one reward.', code='BUDGET_TOO_SMALL',
                             field='budget_minor')
    return budget


def clean_window(starts_at, ends_at):
    start, end = _datetime(starts_at, 'starts_at'), _datetime(ends_at, 'ends_at')
    if end <= start:
        raise InvalidRequest('ends_at must be after starts_at.', code='INVALID_FIELD',
                             field='ends_at')
    return start, end


def _code_taken(config, exclude_pk=None):
    from servers.ride.models import PromoCode

    code = (config or {}).get('code')
    if not code:
        return False
    existing = PromoCode.objects.filter(code__iexact=code)
    if exclude_pk is not None:
        existing = existing.exclude(campaign__pk=exclude_pk)
    if existing.exists():
        return True
    others = Campaign.objects.filter(kind=RIDER_PROMO).exclude(state__in=(CANCELLED, ENDED))
    if exclude_pk:
        others = others.exclude(pk=exclude_pk)
    return any(((c.config or {}).get('code') or '').upper() == code for c in others)


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------

def projection(campaign):
    """What the budget buys: the average discount (or reward) and how many redemptions it funds."""
    cfg = campaign.config or {}
    budget = campaign.budget_minor
    average, basis = None, ''
    if campaign.kind == RIDER_PROMO:
        if cfg.get('discount_type') == 'flat':
            average, basis = int(cfg.get('discount_flat_minor') or 0), 'flat amount'
        else:
            fare = _average_fare_minor(campaign)
            if fare:
                pct = Decimal(str(cfg.get('discount_percent') or '0'))
                average = int((Decimal(fare) * pct / Decimal('100')).to_integral_value())
                cap = cfg.get('max_discount_minor')
                if cap:
                    average = min(average, int(cap))
                basis = 'average fare of the last 30 days in this place'
    elif campaign.kind == DRIVER_INCENTIVE:
        average, basis = int(cfg.get('reward_minor') or 0), 'reward per driver'
    else:
        average = (int(cfg.get('referrer_reward_minor') or 0)
                   + int(cfg.get('referee_reward_minor') or 0))
        basis = 'both rewards per qualified referral'
    out = {'budget_minor': budget, 'average_minor': average, 'basis': basis,
           'max_redemptions': None}
    if budget and average:
        out['max_redemptions'] = budget // average
    cap = cfg.get('max_total_redemptions')
    if cap and out['max_redemptions'] is not None:
        out['max_redemptions'] = min(out['max_redemptions'], int(cap))
    return out


def _average_fare_minor(campaign):
    from servers.ride.models import Trip

    qs = Trip.objects.filter(currency=campaign.currency,
                             requested_at__gte=timezone.now() - timedelta(days=30),
                             estimated_fare_minor__isnull=False)
    if campaign.geo_node is not None:
        qs = qs.filter(zone__geo_node__path__startswith=campaign.geo_node.path)
    value = qs.aggregate(a=Avg('estimated_fare_minor'))['a']
    return int(value) if value else None


def campaign_dict(c, *, detail=False):
    used = c.committed_minor + c.spent_minor
    budget = c.budget_minor
    out = {
        'id': c.id, 'kind': c.kind, 'name': c.name, 'description': c.description,
        'state': c.state, 'country': c.country, 'currency': c.currency,
        'scope': c.geo_node.code if c.geo_node_id else None,
        'scope_name': c.geo_node.name if c.geo_node_id else None,
        'scope_kind': c.geo_node.kind if c.geo_node_id else None,
        'budget': {
            'budget_minor': budget, 'committed_minor': c.committed_minor,
            'spent_minor': c.spent_minor,
            'remaining_minor': c.remaining_minor, 'overshoot_minor': c.overshoot_minor,
            'used_percent': (min(100, round(used * 100 / budget)) if budget else None),
            'currency': c.currency,
        },
        'starts_at': c.starts_at.isoformat() if c.starts_at else None,
        'ends_at': c.ends_at.isoformat() if c.ends_at else None,
        'config': c.config, 'is_legacy': c.is_legacy, 'reason': c.reason,
        'approval_request_id': c.approval_request_id,
        'budget_request_id': c.budget_request_id,
        'created_by': {'id': c.created_by_id, 'email': c.created_by_email,
                       'label': c.created_by_label, 'role': c.created_by_role},
        'created_at': c.created_at.isoformat(),
        'launched_at': c.launched_at.isoformat() if c.launched_at else None,
        'paused_at': c.paused_at.isoformat() if c.paused_at else None,
        'cancelled_at': c.cancelled_at.isoformat() if c.cancelled_at else None,
        'exhausted_at': c.exhausted_at.isoformat() if c.exhausted_at else None,
        'ended_at': c.ended_at.isoformat() if c.ended_at else None,
        'write_capability': write_capability(c.kind, c.config),
        'tax': 'not configured',
    }
    if detail:
        out['projection'] = projection(c)
    return out


# ---------------------------------------------------------------------------
# Drafting
# ---------------------------------------------------------------------------

def _apply_input(actor, campaign, data, *, creating):
    """Validate `data` into `campaign` (not saved)."""
    kind = data.get('kind') if creating else campaign.kind
    if kind not in KINDS:
        raise InvalidRequest('kind must be one of: ' + ', '.join(KINDS), code='INVALID_FIELD',
                             field='kind')
    campaign.kind = kind
    probe_cfg = data.get('config') if isinstance(data.get('config'), dict) else campaign.config
    capability = write_capability(kind, probe_cfg)
    if 'scope' in data or creating:
        node = resolve_node(actor, data.get('scope'), capability)
        campaign.geo_node = node
        campaign.country = country_of_node(node)
        campaign.currency = get_country(campaign.country).currency
    else:
        node = campaign.geo_node
    if creating or 'name' in data:
        name = str(data.get('name') or '').strip()
        if not name:
            raise InvalidRequest('A campaign needs a name.', code='INVALID_FIELD', field='name')
        campaign.name = name[:120]
    if 'description' in data:
        campaign.description = str(data.get('description') or '')[:256]
    if creating or 'config' in data or 'scope' in data:
        campaign.config = clean_config(kind, data.get('config', campaign.config), node)
        # The capability follows the cleaned audience (a referral's depends on it).
        resolve_node(actor, node.code, write_capability(kind, campaign.config))
    if creating or 'budget_minor' in data or 'config' in data:
        campaign.budget_minor = clean_budget(
            data.get('budget_minor', campaign.budget_minor), kind, campaign.config)
    if creating or 'starts_at' in data or 'ends_at' in data:
        campaign.starts_at, campaign.ends_at = clean_window(
            data.get('starts_at', campaign.starts_at.isoformat() if campaign.starts_at else None),
            data.get('ends_at', campaign.ends_at.isoformat() if campaign.ends_at else None))
    if 'reason' in data:
        campaign.reason = str(data.get('reason') or '')[:2000]
    if kind == RIDER_PROMO and _code_taken(campaign.config, campaign.pk):
        raise InvalidRequest('That promo code is already in use.', code='CODE_TAKEN',
                             field='code')


@transaction.atomic
def create_draft(actor, data, *, http_request=None):
    campaign = Campaign(state=DRAFT)
    _apply_input(actor, campaign, data, creating=True)
    campaign.set_created_by(actor)
    campaign.save()
    record_admin_action(http_request, action='promotion_campaign_created',
                        target_type='promotion_campaign', target_id=campaign.id,
                        after=campaign_dict(campaign), reason=campaign.reason)
    return campaign


@transaction.atomic
def update_draft(actor, campaign, data, *, http_request=None):
    require_write(actor, campaign)
    campaign = Campaign.objects.select_for_update(of=('self',)).select_related('geo_node').get(pk=campaign.pk)
    if campaign.state != DRAFT or campaign.is_legacy:
        raise WrongState('Only a draft campaign can be edited.', code='CAMPAIGN_NOT_DRAFT',
                         field='state')
    if 'kind' in data and data['kind'] != campaign.kind:
        raise InvalidRequest('A campaign cannot change kind.', code='INVALID_FIELD', field='kind')
    before = campaign_dict(campaign)
    _apply_input(actor, campaign, data, creating=False)
    campaign.save()
    record_admin_action(http_request, action='promotion_campaign_updated',
                        target_type='promotion_campaign', target_id=campaign.id,
                        before=before, after=campaign_dict(campaign), reason=campaign.reason)
    return campaign


# ---------------------------------------------------------------------------
# Submit for approval
# ---------------------------------------------------------------------------

def _digest(payload):
    raw = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:24]
    return '-'.join(raw[i:i + 6] for i in range(0, 24, 6))


def propose_launch(actor, campaign, *, reason='', http_request=None):
    require_write(actor, campaign)
    reason = (reason or campaign.reason or '').strip()
    if not reason:
        raise InvalidRequest('A reason is required.', code='REASON_REQUIRED', field='reason')
    with transaction.atomic():
        locked = Campaign.objects.select_for_update(of=('self',)).select_related('geo_node').get(pk=campaign.pk)
        if locked.state != DRAFT or locked.is_legacy:
            raise WrongState('Only a draft campaign can be submitted.',
                             code='CAMPAIGN_NOT_DRAFT', field='state')
        if locked.ends_at <= timezone.now():
            raise InvalidRequest('The campaign window has already passed.',
                                 code='WINDOW_PASSED', field='ends_at')
        if locked.kind == RIDER_PROMO and _code_taken(locked.config, locked.pk):
            raise InvalidRequest('That promo code is already in use.', code='CODE_TAKEN',
                                 field='code')
        payload = {
            'campaign_id': locked.id, 'kind': locked.kind, 'name': locked.name,
            'scope': locked.geo_node.code, 'currency': locked.currency,
            'budget_minor': locked.budget_minor,
            'starts_at': locked.starts_at.isoformat(), 'ends_at': locked.ends_at.isoformat(),
            'config': locked.config, 'projection': projection(locked)}
        outcome = require_approval_or_execute(
            PROMOTION_LAUNCH, actor=actor, country=locked.country, scope_node=locked.geo_node,
            target_type='promotion_campaign', target_id=str(locked.id), payload=payload,
            idempotency_key=f'promotion.launch:{locked.id}:{_digest(payload)}', reason=reason,
            http_request=http_request)
        locked.state = PENDING
        locked.reason = reason[:2000]
        locked.approval_request_id = outcome.request.pk
        locked.save(update_fields=['state', 'reason', 'approval_request_id', 'updated_at'])
    record_admin_action(http_request, action='promotion_campaign_submitted',
                        target_type='promotion_campaign', target_id=locked.id,
                        after=campaign_dict(locked), reason=reason)
    return outcome


def _sync_serving_rows(campaign, active):
    """Keep the old row the apps read in step with the campaign's state."""
    if campaign.promo_code_id:
        type(campaign.promo_code).objects.filter(pk=campaign.promo_code_id).update(
            is_active=active)
    if campaign.quest_id:
        type(campaign.quest).objects.filter(pk=campaign.quest_id).update(is_active=active)


def _create_promo_code(campaign, active):
    from servers.ride.models import PromoCode

    cfg = campaign.config
    code = cfg.get('code') or f'AUTO{campaign.pk}'
    zone = _zone(cfg.get('zone_code'), campaign.geo_node) if cfg.get('zone_code') else None
    return PromoCode.objects.create(
        code=code, description=campaign.description or campaign.name,
        discount_type=cfg['discount_type'], currency=campaign.currency,
        discount_percent=(Decimal(cfg['discount_percent']) if cfg.get('discount_percent')
                          else None),
        discount_flat_minor=cfg.get('discount_flat_minor'),
        max_discount_amount_minor=cfg.get('max_discount_minor'),
        min_fare_minor=cfg.get('min_fare_minor') or 0, zone=zone,
        valid_from=campaign.starts_at, valid_to=campaign.ends_at,
        max_total_redemptions=cfg.get('max_total_redemptions'),
        max_per_user_redemptions=cfg.get('max_per_user') or 1, is_active=active)


@registry.register(PROMOTION_LAUNCH)
def execute_launch(request):
    """The checker approved: put EXACTLY the submitted terms in force."""
    with transaction.atomic():
        campaign = (Campaign.objects.select_for_update(of=('self',)).select_related('geo_node')
                    .filter(pk=(request.payload or {}).get('campaign_id')).first())
        if campaign is None:
            raise registry.ExecutionRefused('CAMPAIGN_GONE', 'The campaign no longer exists.')
        if campaign.state != PENDING:
            raise registry.ExecutionRefused('CAMPAIGN_NOT_PENDING',
                                            'The campaign is no longer waiting for approval.')
        sealed = request.payload or {}
        if (sealed.get('budget_minor') != campaign.budget_minor
                or sealed.get('config') != campaign.config):
            raise registry.ExecutionRefused(
                'CAMPAIGN_CHANGED', 'The campaign changed after it was submitted.')
        now = timezone.now()
        if campaign.ends_at <= now:
            raise registry.ExecutionRefused('WINDOW_PASSED', 'The campaign window has passed.')
        starts_now = campaign.starts_at <= now
        if campaign.kind == RIDER_PROMO:
            try:
                with transaction.atomic():
                    campaign.promo_code = _create_promo_code(campaign, active=starts_now)
            except IntegrityError:
                raise registry.ExecutionRefused('CODE_TAKEN', 'That promo code is now in use.')
        campaign.state = LIVE if starts_now else SCHEDULED
        campaign.launched_at = now
        campaign.save()
        record_admin_action(
            None, action='promotion_campaign_launched', target_type='promotion_campaign',
            target_id=campaign.id, after=campaign_dict(campaign),
            reason=request.decision_reason or request.request_reason)
        return {'campaign_id': campaign.id, 'state': campaign.state}


@registry.on_closed(PROMOTION_LAUNCH)
def launch_request_closed(request, outcome):
    campaign = Campaign.objects.select_for_update().filter(
        pk=(request.payload or {}).get('campaign_id')).first()
    if campaign is not None and campaign.state == PENDING:
        campaign.state = DRAFT
        campaign.save(update_fields=['state', 'updated_at'])
        record_admin_action(
            None, action='promotion_campaign_returned_to_draft', target_type='promotion_campaign',
            target_id=campaign.id, after={'outcome': outcome}, reason=f'approval {outcome}')


# ---------------------------------------------------------------------------
# Pause, resume, cancel (no checker: they cannot raise spend)
# ---------------------------------------------------------------------------

def _transition(actor, campaign, *, allowed, new_state, action, stamp, reason, http_request,
                active=None):
    require_write(actor, campaign)
    with transaction.atomic():
        locked = Campaign.objects.select_for_update(of=('self',)).select_related(
            'geo_node', 'promo_code', 'quest').get(pk=campaign.pk)
        if locked.state not in allowed:
            raise WrongState(f'A {locked.state.replace("_", " ")} campaign cannot do that.',
                             code='CAMPAIGN_WRONG_STATE', field='state')
        before = campaign_dict(locked)
        pending = None
        if new_state == CANCELLED and locked.state == PENDING and locked.approval_request_id:
            from servers.approvals.models import REQUESTED, ApprovalRequest
            pending = ApprovalRequest.objects.filter(pk=locked.approval_request_id,
                                                     status=REQUESTED).first()
        locked.state = new_state
        setattr(locked, stamp, timezone.now())
        locked.save()
        if active is not None:
            _sync_serving_rows(locked, active)
        record_admin_action(http_request, action=action, target_type='promotion_campaign',
                            target_id=locked.id, before=before, after=campaign_dict(locked),
                            reason=reason)
    if pending is not None and pending.maker_id == getattr(actor, 'pk', None):
        from servers.approvals import service as approvals

        try:
            approvals.cancel(pending, actor, reason or 'campaign cancelled', http_request)
        except approvals.ApprovalError:
            logger.warning('campaign %s cancelled but its approval request stays open', locked.id)
    return locked


def pause(actor, campaign, *, reason='', http_request=None):
    return _transition(actor, campaign, allowed=(LIVE, SCHEDULED), new_state=PAUSED,
                       action='promotion_campaign_paused', stamp='paused_at', reason=reason,
                       http_request=http_request, active=False)


def resume(actor, campaign, *, reason='', http_request=None):
    require_write(actor, campaign)
    locked = Campaign.objects.get(pk=campaign.pk)
    if locked.state == PAUSED and locked.ends_at and locked.ends_at <= timezone.now():
        raise InvalidRequest('The campaign window has passed.', code='WINDOW_PASSED',
                             field='ends_at')
    now = timezone.now()
    target = LIVE if (locked.starts_at is None or locked.starts_at <= now) else SCHEDULED
    return _transition(actor, campaign, allowed=(PAUSED,), new_state=target,
                       action='promotion_campaign_resumed', stamp='launched_at', reason=reason,
                       http_request=http_request, active=(target == LIVE))


def cancel(actor, campaign, *, reason='', http_request=None):
    return _transition(
        actor, campaign, allowed=(DRAFT, PENDING, SCHEDULED, LIVE, PAUSED, EXHAUSTED),
        new_state=CANCELLED, action='promotion_campaign_cancelled', stamp='cancelled_at',
        reason=reason, http_request=http_request, active=False)


# ---------------------------------------------------------------------------
# Budget increase
# ---------------------------------------------------------------------------

def propose_budget_increase(actor, campaign, new_budget, *, reason='', http_request=None):
    require_write(actor, campaign)
    reason = (reason or '').strip()
    if not reason:
        raise InvalidRequest('A reason is required.', code='REASON_REQUIRED', field='reason')
    new_budget = _int(new_budget, 'budget_minor', minimum=1)
    with transaction.atomic():
        locked = Campaign.objects.select_for_update(of=('self',)).select_related('geo_node').get(pk=campaign.pk)
        if locked.is_legacy or locked.budget_minor is None:
            raise WrongState('A campaign wrapped from an existing row has no budget; launch a '
                             'new campaign instead.', code='CAMPAIGN_NOT_BUDGETED', field='state')
        if locked.state not in (SCHEDULED, LIVE, PAUSED, EXHAUSTED):
            raise WrongState('Only a launched campaign can have its budget raised.',
                             code='CAMPAIGN_WRONG_STATE', field='state')
        if new_budget <= locked.budget_minor:
            raise InvalidRequest('A budget can only be increased.', code='BUDGET_NOT_INCREASED',
                                 field='budget_minor')
        if locked.budget_request_id:
            from servers.approvals.models import REQUESTED, ApprovalRequest
            if ApprovalRequest.objects.filter(pk=locked.budget_request_id,
                                              status=REQUESTED).exists():
                raise WrongState('A budget increase is already waiting for a checker.',
                                 code='BUDGET_REQUEST_OPEN', field='state')
        payload = {'campaign_id': locked.id, 'name': locked.name,
                   'currency': locked.currency, 'from_minor': locked.budget_minor,
                   'to_minor': new_budget}
        outcome = require_approval_or_execute(
            PROMOTION_BUDGET_INCREASE, actor=actor, country=locked.country,
            scope_node=locked.geo_node, target_type='promotion_campaign',
            target_id=str(locked.id), payload=payload,
            idempotency_key=(f'promotion.budget_increase:{locked.id}:{locked.budget_minor}:'
                             f'{new_budget}'),
            reason=reason, http_request=http_request)
        locked.budget_request_id = outcome.request.pk
        locked.save(update_fields=['budget_request_id', 'updated_at'])
    record_admin_action(http_request, action='promotion_budget_increase_requested',
                        target_type='promotion_campaign', target_id=locked.id,
                        after=payload, reason=reason)
    return outcome


@registry.register(PROMOTION_BUDGET_INCREASE)
def execute_budget_increase(request):
    from .budget import min_viable_minor

    payload = request.payload or {}
    with transaction.atomic():
        campaign = (Campaign.objects.select_for_update(of=('self',)).select_related('geo_node')
                    .filter(pk=payload.get('campaign_id')).first())
        if campaign is None:
            raise registry.ExecutionRefused('CAMPAIGN_GONE', 'The campaign no longer exists.')
        if campaign.state in (CANCELLED, ENDED, DRAFT, PENDING):
            raise registry.ExecutionRefused('CAMPAIGN_WRONG_STATE',
                                            'The campaign can no longer take a budget.')
        if campaign.budget_minor != payload.get('from_minor'):
            raise registry.ExecutionRefused('BUDGET_CHANGED',
                                            'The budget changed after the request was raised.')
        before = campaign_dict(campaign)
        campaign.budget_minor = int(payload['to_minor'])
        fields = ['budget_minor', 'updated_at']
        if campaign.state == EXHAUSTED and (
                campaign.budget_minor - campaign.committed_minor - campaign.spent_minor
        ) >= min_viable_minor(campaign) and (campaign.ends_at is None
                                             or campaign.ends_at > timezone.now()):
            campaign.state = LIVE
            campaign.exhausted_at = None
            fields += ['state', 'exhausted_at']
        campaign.save(update_fields=fields)
        record_admin_action(
            None, action='promotion_budget_increased', target_type='promotion_campaign',
            target_id=campaign.id, before=before, after=campaign_dict(campaign),
            reason=request.decision_reason or request.request_reason)
        return {'campaign_id': campaign.id, 'budget_minor': campaign.budget_minor,
                'state': campaign.state}


# ---------------------------------------------------------------------------
# The sweep (beat): time-based transitions and housekeeping
# ---------------------------------------------------------------------------

def sweep(now=None):
    from . import referrals, rider_promos

    now = now or timezone.now()
    started = ended = 0
    for campaign in Campaign.objects.filter(state=SCHEDULED, starts_at__lte=now,
                                            ends_at__gt=now).select_related('promo_code', 'quest'):
        with transaction.atomic():
            updated = Campaign.objects.filter(pk=campaign.pk, state=SCHEDULED).update(
                state=LIVE, updated_at=now)
            if updated:
                _sync_serving_rows(campaign, True)
                started += 1
                record_admin_action(None, action='promotion_campaign_started',
                                    target_type='promotion_campaign', target_id=campaign.pk,
                                    after={'state': LIVE}, reason='start time reached')
    for campaign in Campaign.objects.filter(
            state__in=(SCHEDULED, LIVE, PAUSED, EXHAUSTED), ends_at__lte=now
    ).select_related('promo_code', 'quest'):
        with transaction.atomic():
            updated = Campaign.objects.filter(
                pk=campaign.pk, state__in=(SCHEDULED, LIVE, PAUSED, EXHAUSTED)).update(
                state=ENDED, ended_at=now, updated_at=now)
            if updated:
                _sync_serving_rows(campaign, False)
                ended += 1
                record_admin_action(None, action='promotion_campaign_ended',
                                    target_type='promotion_campaign', target_id=campaign.pk,
                                    after={'state': ENDED}, reason='end time reached')
    released = rider_promos.release_cancelled()
    paid = referrals.retry_qualified()
    return {'started': started, 'ended': ended, 'released': released, 'referrals_paid': paid}


def visible(user):
    return scoped(user, Campaign.objects.select_related('geo_node'))

