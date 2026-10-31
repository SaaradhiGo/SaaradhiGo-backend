"""Driver incentives: a quest of N trips, a fixed reward, paid in the weekly payout.

THE RULINGS (owner, 2026-10-31, AUDIT 27.51)
--------------------------------------------
* The reward is paid in the WEEKLY PAYOUT and carries NO COMMISSION. It is a credit on the
  driver's settlement wallet (ledger purpose `incentive_reward`), which the weekly run sweeps
  into the next payout with everything else; it is its own line in the driver's earnings and in
  the statement, never folded into a trip.
* TAX is go-live blocker 15: nothing here computes or withholds any tax. The earnings response
  says `tax: not configured`, the way settlements already do.
* The budget is a HARD CAP. A reward is RESERVED AT THE MOMENT THE THRESHOLD IS CROSSED
  (`budget.spend_now`): if it fits it is paid; if it does not, the driver's progress is marked
  `unfunded` and nothing is paid. First to cross is first served; a driver who crosses after the
  budget is used earns nothing, which the app can say ("this incentive's budget is used up").

PROGRESS
--------
Counted from COMPLETED trips that match the campaign's filters, once per trip: `IncentiveTrip`
is unique on (campaign, trip), so a replayed completion (a redelivered task, a second hook)
counts nothing twice, and the count is recomputed from those rows, never incremented blind.
A trip an operator ended (`end_rule`) does not count.

FILTERS (all optional, in `campaign.config`)
--------------------------------------------
`zone_code` (the pickup zone), `hour_from`/`hour_to` (local hour of the trip's completion in the
zone's time zone; a window that wraps midnight is allowed), `vehicle_type` (the vehicle class).
A campaign is also limited to its PLACE: only trips whose pickup zone sits under the campaign's
node count, and only drivers whose home city does.
"""

import logging
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.db import IntegrityError, transaction
from django.utils import timezone

from . import budget
from .models import (
    COMPLETED, DRIVER_INCENTIVE, INCENTIVE_REWARD, IN_PROGRESS, LIVE, UNFUNDED, Campaign,
    IncentiveProgress, IncentiveTrip, LedgerEntry,
)

logger = logging.getLogger(__name__)

REWARD_PURPOSE = INCENTIVE_REWARD


def _zone_tz(trip):
    from base.countries import get_country

    zone = trip.zone
    name = getattr(zone, 'timezone_name', '') if zone is not None else ''
    if name:
        try:
            return ZoneInfo(name)
        except ZoneInfoNotFoundError:
            pass
    country = get_country(getattr(zone, 'country', None))
    return ZoneInfo(country.default_timezone)


def in_hours(config, trip, at):
    hour_from, hour_to = config.get('hour_from'), config.get('hour_to')
    if hour_from is None or hour_to is None:
        return True
    hour = at.astimezone(_zone_tz(trip)).hour
    if hour_from <= hour_to:
        return hour_from <= hour < hour_to
    return hour >= hour_from or hour < hour_to      # wraps midnight (22 -> 6)


def trip_matches(campaign, trip, at):
    """Does this completed trip count toward `campaign`? (Place, window and filters.)"""
    cfg = campaign.config or {}
    if getattr(trip, 'end_rule', ''):
        return False
    if campaign.starts_at and at < campaign.starts_at:
        return False
    if campaign.ends_at and at >= campaign.ends_at:
        return False
    node = campaign.geo_node
    zone = trip.zone
    if node is not None:
        zone_node = getattr(zone, 'geo_node', None) if zone is not None else None
        if zone_node is None or not str(zone_node.path).startswith(node.path):
            return False
    if cfg.get('zone_code') and (zone is None or zone.code != cfg['zone_code']):
        return False
    if cfg.get('vehicle_type'):
        vehicle = getattr(trip, 'vehicle_id', None)
        vtype = getattr(getattr(vehicle, 'vehicle_type_id', None), 'type', '') if vehicle else ''
        if str(vtype).lower() != str(cfg['vehicle_type']).lower():
            return False
    return in_hours(cfg, trip, at)


def driver_in_place(campaign, driver):
    node = campaign.geo_node
    if node is None:
        return True
    home = driver.home_city
    return home is not None and str(home.path).startswith(node.path)


def on_trip_completed(trip):
    """Count `trip` toward every live incentive it matches. Idempotent per (campaign, trip)."""
    if trip.driver_id_id is None:
        return []
    driver = trip.driver_id
    at = trip.completed_at or timezone.now()
    touched = []
    campaigns = (Campaign.objects.filter(kind=DRIVER_INCENTIVE, state=LIVE, is_legacy=False)
                 .select_related('geo_node'))
    for campaign in campaigns:
        if not driver_in_place(campaign, driver) or not trip_matches(campaign, trip, at):
            continue
        progress = count_trip(campaign, driver, trip)
        if progress is not None:
            touched.append(progress)
    return touched


def count_trip(campaign, driver, trip):
    """Record the trip, recompute the count, pay the reward if the threshold was crossed."""
    with transaction.atomic():
        progress, _ = IncentiveProgress.objects.select_for_update().get_or_create(
            campaign=campaign, driver=driver, defaults={'currency': campaign.currency})
        try:
            with transaction.atomic():
                IncentiveTrip.objects.create(campaign=campaign, driver=driver, trip=trip)
        except IntegrityError:
            return None                      # already counted: a replay changes nothing
        progress.trips_counted = IncentiveTrip.objects.filter(
            campaign=campaign, driver=driver).count()
        progress.save(update_fields=['trips_counted', 'updated_at'])
        target = int((campaign.config or {}).get('target_trips') or 0)
        crossed = progress.status == IN_PROGRESS and target and progress.trips_counted >= target
        if crossed:
            _pay_reward(campaign, driver, progress)
    _notify(campaign, driver, progress)
    return progress


def _pay_reward(campaign, driver, progress):
    """Reserve and pay the reward, inside the progress lock. `unfunded` if the budget is used."""
    from servers.rider.models import Wallet, WalletTransaction, get_wallet

    reward = int((campaign.config or {}).get('reward_minor') or 0)
    try:
        budget.spend_now(campaign.pk, reward)
    except budget.Refused:
        progress.status = UNFUNDED
        progress.completed_at = timezone.now()
        progress.save(update_fields=['status', 'completed_at', 'updated_at'])
        return
    wallet = get_wallet(driver.user_id, Wallet.SCOPE_DRIVER, lock=True)
    key = f'incentive:{campaign.pk}:{driver.pk}'
    txn = WalletTransaction.objects.create(
        user_id=driver.user_id, currency=campaign.currency, amount_minor=reward,
        txn_type='credit', status='completed', purpose=REWARD_PURPOSE,
        reference_id=f'INCENTIVE_{campaign.pk}', idempotency_key=key)
    wallet.balance_minor = wallet.balance_minor + reward
    wallet.save(update_fields=['balance_minor'])
    LedgerEntry.objects.create(
        kind=INCENTIVE_REWARD, campaign=campaign, user=driver.user_id, amount_minor=reward,
        currency=campaign.currency, wallet_transaction=txn, idempotency_key=key)
    progress.status = COMPLETED
    progress.reward_minor = reward
    progress.completed_at = timezone.now()
    progress.save(update_fields=['status', 'reward_minor', 'completed_at', 'updated_at'])


def _notify(campaign, driver, progress):
    """Progress and completion go through the notification dispatcher (never a direct push)."""
    from servers.notifications import dispatcher

    cfg = campaign.config or {}
    target = int(cfg.get('target_trips') or 0)
    if progress.status == COMPLETED:
        event, context = 'promotions.incentive_completed', {
            'name': campaign.name, 'reward': _money(progress.reward_minor, campaign.currency)}
    elif progress.status == UNFUNDED:
        return
    else:
        event, context = 'promotions.incentive_progress', {
            'name': campaign.name, 'done': str(progress.trips_counted), 'target': str(target)}
    try:
        dispatcher.notify(event, driver.user_id, context,
                          idempotency_key=f'{event}:{campaign.pk}:{driver.pk}:{progress.trips_counted}',
                          source=event)
    except Exception:  # noqa: BLE001 - a notification must never undo a reward
        logger.exception('incentive notification failed campaign=%s driver=%s',
                         campaign.pk, driver.pk)


def _money(minor, currency):
    from base.money import format_money, from_minor

    return format_money(from_minor(minor, currency), currency)


# ---------------------------------------------------------------------------
# What the driver app reads
# ---------------------------------------------------------------------------

INCENTIVE_REWARD_KIND = 'incentive_reward'


def driver_view(driver, now=None):
    """Live (and the driver's recently completed) incentives with this driver's progress."""
    now = now or timezone.now()
    rows = []
    campaigns = (Campaign.objects.filter(kind=DRIVER_INCENTIVE, is_legacy=False)
                 .filter(state__in=(LIVE, 'exhausted', 'paused'))
                 .select_related('geo_node'))
    mine = {p.campaign_id: p for p in IncentiveProgress.objects.filter(driver=driver)}
    from servers.fraud.models import RewardHold

    held_campaigns = set(RewardHold.objects.filter(
        user=driver.user_id, status=RewardHold.HELD, kind=INCENTIVE_REWARD_KIND).values_list(
        'ledger_entry__campaign_id', flat=True))
    for campaign in campaigns:
        if not driver_in_place(campaign, driver):
            continue
        progress = mine.get(campaign.pk)
        cfg = campaign.config or {}
        target = int(cfg.get('target_trips') or 0)
        status = progress.status if progress else IN_PROGRESS
        rows.append({
            'id': campaign.pk, 'name': campaign.name, 'description': campaign.description,
            'state': campaign.state, 'target_trips': target,
            'progress_trips': progress.trips_counted if progress else 0,
            'status': status,
            'reward_minor': int(cfg.get('reward_minor') or 0), 'currency': campaign.currency,
            'reward': _money(int(cfg.get('reward_minor') or 0), campaign.currency),
            'starts_at': campaign.starts_at.isoformat() if campaign.starts_at else None,
            'ends_at': campaign.ends_at.isoformat() if campaign.ends_at else None,
            'filters': {k: cfg.get(k) for k in ('zone_code', 'hour_from', 'hour_to',
                                                'vehicle_type') if cfg.get(k) not in (None, '')},
            'budget_used_up': status == UNFUNDED or campaign.state == 'exhausted',
            # A reward held for review reads "pending review", never why (additive).
            'reward_status': 'pending_review' if campaign.pk in held_campaigns else None,
            'reward_status_label': 'Pending review' if campaign.pk in held_campaigns else None,
            'commission': 'none', 'tax': 'not configured',
        })
    return rows


def incentive_lines(driver):
    """The driver's incentive and referral rewards as their own lines (additive keys for the
    earnings summary). Totals of the ledger credits, in the driver's wallet currency. No
    commission is taken on them and no tax is computed (go-live blocker 15)."""
    from django.db.models import Sum

    from base.money import from_minor
    from servers.rider.models import WalletTransaction

    rows = (WalletTransaction.objects
            .filter(user_id=driver.user_id, status='completed',
                    purpose__in=('incentive_reward', 'referral_reward'))
            .values('purpose', 'currency').annotate(total=Sum('amount_minor')))
    out = {'incentive_rewards': '0.00', 'referral_rewards': '0.00',
           'incentive_commission': 'none', 'incentive_tax': 'not configured'}
    for row in rows:
        key = 'incentive_rewards' if row['purpose'] == 'incentive_reward' else 'referral_rewards'
        out[key] = str(from_minor(row['total'] or 0, row['currency']))
    # AUDIT 27.56: a reward pending review, and a recovery of a reward, each its own neutral line.
    from servers.fraud import money as fraud_money

    out.update(fraud_money.driver_money_lines(driver))
    return out
