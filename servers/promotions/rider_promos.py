"""Rider promos: the platform pays the discount, the driver's economics do not move.

THE RULING (owner, 2026-10-31, AUDIT 27.51)
-------------------------------------------
A rider promo is PLATFORM-FUNDED. Driver earnings and commission are calculated on the FULL
fare. A promo never reduces what a driver earns.

HOW THAT IS ENFORCED
--------------------
`Trip.estimated_fare` / `final_fare` stay the FULL fare. Settlement (`driver.utils.credit_driver_wallet`)
reads them for the gross, the commission and the driver's net, so those numbers cannot see a
promo at all. What the rider is CHARGED is `payable_minor(trip)`: the fare minus the discount
reserved on that trip. Every place that charges a rider (create-order, the payment switch, the
payment retry, the payment row written at completion) asks `payable_minor`.

At settlement, `settle_trip` (called from inside the settlement transaction, after the
`TripSettlement` row is written):

  * moves the discount from `committed` to `spent` on the campaign's budget;
  * writes a `promo_discount_platform_funded` ledger entry (the platform's cost);
  * for a CASH trip, where the driver collected the fare less the discount in hand, credits
    the driver's wallet with the discount (same ledger purpose), so the driver ends up with
    exactly what they would have without the promo. For an online or wallet trip nothing moves
    in the driver's balance: the platform collected less from the rider and still owes the
    driver the full net.

A trip that never settles (cancelled, or ended by an operator) releases its reservation.

WHAT A BOOKED TRIP IS OWED
--------------------------
`reserve_for_redemption` is the only place a NEW application is refused for budget. Once the
`PromoRedemption` row exists the trip is honoured: `settle_trip` and `release_for_trip` work
whatever state the campaign is in.
"""

import logging

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from base.money import from_minor, to_minor

from . import budget
from .models import (
    DISCOUNT_FUNDED, LIVE, RIDER_PROMO, Campaign, LedgerEntry,
)

logger = logging.getLogger(__name__)

LEDGER_PURPOSE = DISCOUNT_FUNDED


# ---------------------------------------------------------------------------
# The campaign behind a code
# ---------------------------------------------------------------------------

def campaign_for(promo):
    return Campaign.objects.filter(promo_code=promo).first()


def _in_place(campaign, zone):
    """Is the pickup zone inside the campaign's place? A campaign anchored at a city serves
    that city's zones; one anchored at the country serves any zone of the country. With no zone
    known only a country-level (or unanchored) campaign serves."""
    node = campaign.geo_node
    if node is None or node.kind in ('country', 'global'):
        if zone is None or node is None:
            return True
        zone_node = getattr(zone, 'geo_node', None)
        return zone_node is None or str(zone_node.path).startswith(node.path)
    if zone is None:
        return False
    zone_node = getattr(zone, 'geo_node', None)
    return zone_node is not None and str(zone_node.path).startswith(node.path)


def available_in_market(country_code, city_node=None, at=None):
    """Is there a rider promo a rider in this market could use right now?

    The app-config `promos_available` flag: the rider app shows its promo-code entry only when
    this is true. True when a `live` rider-promo campaign anchored at the country (or the named
    city, or any ancestor of it) is inside its window, or a still-active promo code that was never
    wrapped in a campaign is. A paused, ended, exhausted or draft campaign does not count. This
    only SHOWS an entry: whether a given code applies is still decided at apply and booking.
    """
    from servers.ride.models import PromoCode

    now = at or timezone.now()
    campaigns = (Campaign.objects.filter(kind=RIDER_PROMO, state=LIVE, country=country_code)
                 .filter(Q(starts_at__isnull=True) | Q(starts_at__lte=now))
                 .filter(Q(ends_at__isnull=True) | Q(ends_at__gt=now))
                 .select_related('geo_node'))
    for campaign in campaigns:
        node = campaign.geo_node
        if (city_node is None or node is None or node.kind in ('country', 'global')
                or str(city_node.path).startswith(str(node.path))):
            return True
    from base.countries import find_country

    country = find_country(country_code)
    if country is None:
        return False
    codes = (PromoCode.objects.filter(is_active=True, campaign__isnull=True,
                                      valid_from__lte=now, valid_to__gt=now,
                                      currency=country.currency)
             .select_related('zone__geo_node'))
    for promo in codes:
        zone_node = getattr(promo.zone, 'geo_node', None) if promo.zone_id else None
        if zone_node is None and promo.zone_id and city_node is not None:
            continue
        if (zone_node is None or city_node is None
                or str(city_node.path).startswith(str(zone_node.path))):
            return True
    return False


def preview_refusal(promo, user, discount, zone):
    """The refusal code for the apply PREVIEW (read-only), or ''."""
    campaign = campaign_for(promo)
    if campaign is None:
        return ''
    if campaign.state != LIVE:
        return 'PROMO_NOT_FOUND'
    if not _in_place(campaign, zone):
        return 'PROMO_WRONG_ZONE'
    if (campaign.config or {}).get('new_riders_only') and _has_completed_trip(user):
        return 'PROMO_NEW_RIDERS_ONLY'
    minor = to_minor(discount, promo.currency)
    if not budget.admits(campaign, minor):
        return 'PROMO_BUDGET_EXHAUSTED'
    return ''


def reserve_for_redemption(promo, user, discount, zone):
    """`(campaign, refusal)` for a redemption. Reserves the discount, atomically.

    A promo with no campaign (a code created outside the module) is wrapped on the spot as a
    legacy campaign so every redemption has a budget line to settle against.
    """
    campaign = campaign_for(promo) or ensure_legacy_campaign(promo)
    if campaign is None:
        return None, ''
    if campaign.state != LIVE:
        return None, 'PROMO_NOT_FOUND'
    if not _in_place(campaign, zone):
        return None, 'PROMO_WRONG_ZONE'
    if (campaign.config or {}).get('new_riders_only') and _has_completed_trip(user):
        return None, 'PROMO_NEW_RIDERS_ONLY'
    try:
        budget.reserve(campaign.pk, to_minor(discount, promo.currency))
    except budget.Refused as exc:
        return None, ('PROMO_BUDGET_EXHAUSTED' if exc.code == budget.BUDGET_EXHAUSTED
                      else 'PROMO_NOT_FOUND')
    return campaign, ''


def _has_completed_trip(user):
    from servers.ride.models import Trip

    return Trip.objects.filter(user_id=user, status_id__status_code='completed').exists()


def ensure_legacy_campaign(promo):
    """Wrap a `PromoCode` that has no campaign. No budget, no checker: it behaves as it did."""
    from servers.admin_access.models import GeoNode

    now = timezone.now()
    node = None
    if promo.zone_id and promo.zone.geo_node_id:
        node = promo.zone.geo_node
    if node is None:
        from base.countries import COUNTRIES

        code = next((c.code for c in COUNTRIES.values() if c.currency == promo.currency), '')
        node = GeoNode.objects.filter(kind='country', code=code).first()
    country = ''
    if node is not None:
        country = node.country.code if node.kind != 'country' and node.country else node.code
    live = promo.is_active and promo.valid_from <= now < promo.valid_to
    state = LIVE if live else ('ended' if promo.valid_to <= now else 'paused')
    campaign, _ = Campaign.objects.get_or_create(
        promo_code=promo,
        defaults=dict(
            kind=RIDER_PROMO, name=promo.code, description=promo.description[:256],
            state=state, country=country[:2], geo_node=node, currency=promo.currency,
            budget_minor=None, starts_at=promo.valid_from, ends_at=promo.valid_to,
            config=legacy_promo_config(promo), is_legacy=True,
            reason='Wrapped from an existing promo code (module 12).'))
    return campaign


def legacy_promo_config(promo):
    return {
        'code': promo.code, 'auto_apply': False, 'discount_type': promo.discount_type,
        'discount_percent': str(promo.discount_percent) if promo.discount_percent is not None else None,
        'discount_flat_minor': promo.discount_flat_minor,
        'max_discount_minor': promo.max_discount_amount_minor,
        'min_fare_minor': promo.min_fare_minor,
        'max_per_user': promo.max_per_user_redemptions,
        'max_total_redemptions': promo.max_total_redemptions,
        'zone_code': promo.zone.code if promo.zone_id else '',
        'new_riders_only': False,
    }


# ---------------------------------------------------------------------------
# What the rider pays
# ---------------------------------------------------------------------------

def _held(trip):
    from servers.ride.models import PromoRedemption

    return PromoRedemption.objects.filter(
        trip=trip, state__in=(PromoRedemption.STATE_COMMITTED, PromoRedemption.STATE_SETTLED,
                              PromoRedemption.STATE_LEGACY))


def discount_minor(trip):
    """The promo discount held on this trip, in minor units (0 if none). A trip an operator ended
    under the PLATFORM-FUNDED rule charges the rider nothing, so there is nothing to discount; under
    the standard rule the promo is honoured (re-worked on the capped fare, `apply_operator_end`)."""
    if getattr(trip, 'end_rule', '') == 'platform_funded':
        return 0
    return sum(r.discount_amount_minor for r in _held(trip))


def fare_minor(trip):
    """The FULL fare: what driver earnings and commission are computed on."""
    return trip.final_fare_minor or trip.estimated_fare_minor or 0


def payable_minor(trip):
    """What the RIDER is charged: the full fare less the promo discount, never below zero."""
    full = fare_minor(trip)
    return max(0, full - min(discount_minor(trip), full))


def is_fully_covered(trip):
    """A promo covered the whole fare: the rider is charged nothing, and there is a fare to cover."""
    return bool(discount_minor(trip)) and fare_minor(trip) > 0 and payable_minor(trip) == 0


def operator_end_discount_minor(trip, capped_minor):
    """The discount a promo held on this trip gives on the CAPPED fare of an operator end, under the
    promo's own terms (a percentage of the capped fare up to its maximum; a flat amount up to the
    capped fare). 0 if the trip holds none."""
    from base.money import from_minor as _fm, to_minor as _tm
    from servers.ride.promos import _compute_discount

    total = 0
    for r in _held(trip):
        promo = r.promo
        fare = _fm(capped_minor, promo.currency)
        total += _tm(_compute_discount(promo, fare), promo.currency)
    return min(total, capped_minor)


def apply_operator_end(trip, capped_minor, rule, plan_discount_minor=None):
    """Settle the promo of a trip an operator ended. PLATFORM-FUNDED: the rider pays 0, so the
    reservation is released. STANDARD: the booked trip is honoured, the discount re-worked on the
    capped fare and the reservation moved to that amount (settled later like any promo trip).

    `plan_discount_minor` (US, AUDIT 27.59 ruling 2): the discount the plan arrived at once the tax is on
    the fare after the discount; it can only be lower than the promo's own amount on the capped fare
    (it is capped at what the rider would otherwise pay). None everywhere else."""
    if rule == 'platform_funded':
        return release_for_trip(trip)
    from servers.ride.models import PromoRedemption

    left = plan_discount_minor
    with transaction.atomic():
        for r in PromoRedemption.objects.select_for_update().select_related('promo').filter(
                trip=trip, state=PromoRedemption.STATE_COMMITTED):
            new = min(operator_end_discount_minor_one(r, capped_minor), capped_minor)
            if left is not None:
                new = min(new, left)
                left -= new
            if r.campaign_id is not None and new != r.discount_amount_minor:
                budget.adjust_reservation(r.campaign_id, r.discount_amount_minor, new)
            r.discount_amount_minor = new
            r.save(update_fields=['discount_amount_minor'])
    return discount_minor(trip)


def operator_end_discount_minor_one(redemption, capped_minor):
    from base.money import from_minor as _fm, to_minor as _tm
    from servers.ride.promos import _compute_discount

    promo = redemption.promo
    return _tm(_compute_discount(promo, _fm(capped_minor, promo.currency)), promo.currency)


def payable(trip):
    """`payable_minor` as a Decimal in the trip's currency."""
    from base.money import currency_for_trip

    return from_minor(payable_minor(trip), currency_for_trip(trip))


# ---------------------------------------------------------------------------
# Booking
# ---------------------------------------------------------------------------

class PromoRefused(Exception):
    """The code the rider entered cannot be applied. Carries the PROMO_* reason."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def find_auto_promo(user, fare, zone, at=None, tax=None):
    """The best live auto-applied rider promo for this booking, or None.

    Best = the largest discount. Only campaigns that are `live`, flagged `auto_apply`, in the
    pickup's place and whose own rules (window, minimum fare, per-rider limit, budget) admit.
    """
    from servers.ride.promos import apply_promo

    best = None
    for campaign in (Campaign.objects.filter(kind=RIDER_PROMO, state=LIVE, is_legacy=False,
                                             promo_code__isnull=False)
                     .select_related('promo_code', 'geo_node')):
        if not (campaign.config or {}).get('auto_apply'):
            continue
        result = apply_promo(campaign.promo_code.code, user, fare, zone=zone, at=at, tax=tax)
        if result.ok and (best is None or result.discount_amount > best.discount_amount):
            best = result
    return best.code if best else None


def book(user, trip, fare, zone, code, tax=None):
    """Redeem `code` (or the best auto-applied promo when `code` is empty) for a new trip.

    Call inside the booking transaction. Returns the `PromoResult`, or None when there is no
    promo. Raises `PromoRefused` if the rider ASKED for a code and it cannot be applied, so the
    booking is refused rather than silently charged more than the rider was shown.

    `tax` is `(percent, fee)` for an additive-tax quote (the US), or False for an inclusive one:
    the tax is then charged on the fare after the discount (AUDIT 27.59 ruling 2; US accountant to
    confirm, blocker #15) and the trip's own fare drops by the tax no longer charged.
    """
    from servers.ride.promos import redeem_promo

    asked = bool((code or '').strip())
    code = (code or '').strip() or find_auto_promo(user, fare, zone, tax=tax)
    if not code:
        return None
    result = redeem_promo(code, user, trip, fare, zone=zone, tax=tax)
    if not result.ok:
        if asked:
            raise PromoRefused(result.reason or 'PROMO_INVALID')
        return None
    return result


# ---------------------------------------------------------------------------
# Settlement and release
# ---------------------------------------------------------------------------

def settle_trip(trip, driver_wallet, *, is_cash):
    """Move the trip's reserved discount to spent and record the platform-funded entry.

    Call inside the settlement transaction, with `driver_wallet` the LOCKED driver wallet.
    Idempotent: a redemption already settled is skipped, and the ledger key is unique.
    """
    from servers.ride.models import PromoRedemption
    from servers.rider.models import WalletTransaction

    redemptions = list(PromoRedemption.objects.select_for_update().filter(
        trip=trip, state=PromoRedemption.STATE_COMMITTED))
    if not redemptions or getattr(trip, 'end_rule', '') == 'platform_funded':
        return 0
    total = 0
    for redemption in redemptions:
        amount = redemption.discount_amount_minor
        campaign_id = redemption.campaign_id
        if campaign_id is None:
            continue
        budget.settle(campaign_id, amount)
        redemption.state = PromoRedemption.STATE_SETTLED
        redemption.settled_at = timezone.now()
        redemption.save(update_fields=['state', 'settled_at'])
        entry = LedgerEntry(
            kind=DISCOUNT_FUNDED, campaign_id=campaign_id, user=trip.user_id, trip=trip,
            amount_minor=amount, currency=redemption.currency,
            idempotency_key=f'promo_discount:{redemption.pk}')
        if is_cash and trip.driver_id_id is not None:
            # The driver collected the fare less the discount; the platform pays the rest.
            txn = WalletTransaction.objects.create(
                user_id=trip.driver_id.user_id, currency=redemption.currency,
                amount_minor=amount, txn_type='credit', status='completed',
                purpose=LEDGER_PURPOSE, reference_id=f'TRIP_{trip.id}',
                idempotency_key=f'TRIP_{trip.id}_PROMO_{redemption.pk}')
            driver_wallet.balance_minor = driver_wallet.balance_minor + amount
            driver_wallet.save(update_fields=['balance_minor'])
            entry.wallet_transaction = txn
        entry.save()
        total += amount
    return total


def release_for_trip(trip):
    """A trip that will never settle gives its reservation back. Idempotent; returns the amount."""
    from servers.ride.models import PromoRedemption

    released = 0
    with transaction.atomic():
        for redemption in PromoRedemption.objects.select_for_update().filter(
                trip=trip, state=PromoRedemption.STATE_COMMITTED):
            if redemption.campaign_id is not None:
                budget.release(redemption.campaign_id, redemption.discount_amount_minor)
            redemption.state = PromoRedemption.STATE_RELEASED
            redemption.released_at = timezone.now()
            redemption.save(update_fields=['state', 'released_at'])
            released += redemption.discount_amount_minor
    return released


def release_cancelled(limit=500):
    """The sweep: release every committed reservation whose trip was cancelled (or never
    created). Returns the number released."""
    from servers.ride.models import PromoRedemption, Trip

    n = 0
    stale = (PromoRedemption.objects
             .filter(state=PromoRedemption.STATE_COMMITTED)
             .filter(Q(trip__status_id__status_code__in=('cancelled', 'canceled'))
                     | Q(trip__end_rule='platform_funded'))
             .values_list('trip_id', flat=True).distinct()[:limit])
    for trip in Trip.objects.filter(pk__in=list(stale)):
        if release_for_trip(trip):
            n += 1
    return n


def dec(minor, currency):
    """A minor-unit amount as a Decimal in `currency` (for the apps and the receipts)."""
    return from_minor(minor, currency)
