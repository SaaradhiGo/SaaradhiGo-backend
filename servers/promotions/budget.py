"""The campaign budget: reserve, settle, release, atomically.

Every operation takes the campaign row's lock (`select_for_update`), so two riders racing for
the last of a budget are serialised: the second sees what the first took. The lock is a real
row lock on PostgreSQL (the postgres-marked contention test proves it); on SQLite, which has no
row locks, writers are serialised by the database file itself.

THE RULES
---------
* ADMISSION: a NEW reservation needs the campaign `live` and the budget to allow it. With
  `strict_cap` (the default) the whole amount must fit in what is left; without it any
  positive remainder admits, and the reservation may cross the cap by less than one amount.
* HONOUR: a reservation that exists is never refused later. `settle` and `release` work in any
  campaign state, including paused, exhausted, ended and cancelled.
* OVERSHOOT: when `committed + spent` stands above the budget, `overshoot_minor` keeps the
  peak and a `promotion.budget_overshoot` alert (warning, Finance) is raised, once per campaign
  while open.
* EXHAUSTION: a live campaign whose remainder can no longer admit another application becomes
  `exhausted` and raises `promotion.budget_exhausted` (info), once. For a strict campaign that is
  a remainder below the smallest amount it could be asked for; for a loose one, nothing left.

Nothing here moves money in a wallet; it counts. The wallet movements live next to the thing
they pay for (`rider_promos`, `incentives`, `referrals`).
"""

import logging

from django.db import transaction
from django.utils import timezone

from .models import EXHAUSTED, LIVE, Campaign

logger = logging.getLogger(__name__)

BUDGET_EXHAUSTED = 'BUDGET_EXHAUSTED'
NOT_LIVE = 'CAMPAIGN_NOT_LIVE'


class Refused(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def min_viable_minor(campaign):
    """The smallest amount this campaign could still be asked to fund. Below this remainder it
    can never admit another application, so it is exhausted rather than left 'live' and dead."""
    cfg = campaign.config or {}
    if campaign.kind == 'rider_promo':
        if cfg.get('discount_type') == 'flat':
            return max(1, int(cfg.get('discount_flat_minor') or 1))
        return 1
    if campaign.kind == 'driver_incentive':
        return max(1, int(cfg.get('reward_minor') or 1))
    return max(1, int(cfg.get('referrer_reward_minor') or 0)
               + int(cfg.get('referee_reward_minor') or 0)) if campaign.kind == 'referral' else 1


def admits(campaign, amount_minor):
    """Would a NEW reservation of `amount_minor` be admitted right now? (No lock, no write: the
    preview the rider sees, and the check a reservation repeats under the lock.)"""
    if campaign.state != LIVE:
        return False
    if campaign.budget_minor is None:
        return True
    used = campaign.committed_minor + campaign.spent_minor
    if campaign.strict_cap:
        return used + amount_minor <= campaign.budget_minor
    return used < campaign.budget_minor


def _alert(campaign, kind, title, severity):
    try:
        from servers.command_centre.services import raise_alert

        return raise_alert(
            kind, title=title[:200], severity=severity,
            payload={'campaign_id': campaign.pk, 'campaign_kind': campaign.kind,
                     'currency': campaign.currency, 'budget_minor': campaign.budget_minor,
                     'committed_minor': campaign.committed_minor,
                     'spent_minor': campaign.spent_minor,
                     'overshoot_minor': campaign.overshoot_minor},
            source_type=kind, source_id=str(campaign.pk), city=campaign.geo_node)
    except Exception:  # noqa: BLE001 - an alert must never fail the money it reports on
        logger.exception('promotion alert %s failed for campaign %s', kind, campaign.pk)
        return None


def _after_change(campaign):
    """Under the lock, after `committed`/`spent` moved: record overshoot, flip to exhausted."""
    from servers.command_centre.models import (
        INFO, PROMOTION_BUDGET_EXHAUSTED, PROMOTION_BUDGET_OVERSHOOT, WARNING,
    )

    fields = ['committed_minor', 'spent_minor', 'updated_at']
    if campaign.budget_minor is None:
        campaign.save(update_fields=fields)
        return
    used = campaign.committed_minor + campaign.spent_minor
    raise_overshoot = False
    if used > campaign.budget_minor:
        over = used - campaign.budget_minor
        if over > campaign.overshoot_minor:
            campaign.overshoot_minor = over
            fields.append('overshoot_minor')
            raise_overshoot = True
    raise_exhausted = False
    # A strict campaign can admit nothing once the remainder cannot fund the smallest amount it
    # could be asked for; a loose one admits while anything is left.
    floor = min_viable_minor(campaign) if campaign.strict_cap else 1
    if campaign.state == LIVE and (campaign.budget_minor - used) < floor:
        campaign.state = EXHAUSTED
        campaign.exhausted_at = timezone.now()
        fields += ['state', 'exhausted_at']
        raise_exhausted = True
    campaign.save(update_fields=fields)
    if raise_overshoot:
        _alert(campaign, PROMOTION_BUDGET_OVERSHOOT,
               f'Campaign "{campaign.name}" is {campaign.overshoot_minor} {campaign.currency} '
               f'(minor units) over its budget: trips that already held a discount were honoured',
               WARNING)
    if raise_exhausted:
        _alert(campaign, PROMOTION_BUDGET_EXHAUSTED,
               f'Campaign "{campaign.name}" has used its whole budget and stopped',
               INFO)


@transaction.atomic
def reserve(campaign_id, amount_minor):
    """Commit `amount_minor` of the budget. Returns the locked, updated campaign.

    Raises `Refused(NOT_LIVE)` or `Refused(BUDGET_EXHAUSTED)`; nothing is written then.
    """
    campaign = Campaign.objects.select_for_update().get(pk=campaign_id)
    if campaign.state != LIVE:
        raise Refused(NOT_LIVE)
    if not admits(campaign, amount_minor):
        raise Refused(BUDGET_EXHAUSTED)
    campaign.committed_minor += amount_minor
    _after_change(campaign)
    return campaign


@transaction.atomic
def settle(campaign_id, amount_minor):
    """The reserved amount became real spend. Works in any state (the trip is honoured)."""
    campaign = Campaign.objects.select_for_update().get(pk=campaign_id)
    campaign.committed_minor = max(0, campaign.committed_minor - amount_minor)
    campaign.spent_minor += amount_minor
    _after_change(campaign)
    return campaign


@transaction.atomic
def release(campaign_id, amount_minor):
    """A reservation that will never settle (a cancelled trip) goes back to the budget."""
    campaign = Campaign.objects.select_for_update().get(pk=campaign_id)
    campaign.committed_minor = max(0, campaign.committed_minor - amount_minor)
    campaign.save(update_fields=['committed_minor', 'updated_at'])
    return campaign


@transaction.atomic
def spend_now(campaign_id, amount_minor):
    """Reserve and settle in one step (an incentive or referral reward paid at once).

    Raises `Refused` exactly like `reserve`.
    """
    campaign = Campaign.objects.select_for_update().get(pk=campaign_id)
    if campaign.state != LIVE:
        raise Refused(NOT_LIVE)
    if not admits(campaign, amount_minor):
        raise Refused(BUDGET_EXHAUSTED)
    campaign.spent_minor += amount_minor
    _after_change(campaign)
    return campaign
