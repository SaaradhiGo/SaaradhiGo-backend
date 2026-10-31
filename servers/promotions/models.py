"""Promotions & Incentives (module 12): one campaign model, three kinds.

    rider_promo       a discount code, or an auto-applied discount, on a rider's booking
    driver_incentive  a quest: N completed trips in a window (with optional filters) -> a reward
    referral          rider->rider or driver->driver: a reward after the referee completes N trips

WHY A NEW APP, NOT MORE COLUMNS ON `ride.PromoCode` / `driver.IncentiveQuest`
------------------------------------------------------------------------------
The three kinds share what matters (a state machine with a checker, a budget with a hard cap,
an owner scope, an audit trail) and differ in everything else. `PromoCode` stays what the booking
path serves (a code, a window, a discount) and `IncentiveQuest` stays what the driver app reads.
A `Campaign` WRAPS them: `promo_code` / `quest` link the old rows (an additive data
migration creates one campaign per existing row; nothing is deleted or changed), and a NEW
rider promo creates its `PromoCode` when the checker approves. So the existing apply-promo and
driver-incentives endpoints keep working unchanged.

STATES
------
    draft -> pending_approval -> scheduled | live -> paused -> live
                                            |  \\-> exhausted (budget) -> live (after an approved increase)
                                            \\----> ended (window passed) | cancelled

BUDGET (minor units of the campaign currency)
---------------------------------------------
`committed` is discount reserved on booked trips, not yet settled. `spent` is settled. A NEW
application is admitted only while the budget allows it (`budget.py`); a trip that already holds
a reservation is ALWAYS honoured, even if the campaign is meanwhile paused, exhausted or
cancelled. `overshoot_minor` records the most `committed + spent` has stood above the budget.
"""

from django.conf import settings
from django.db import models
from django.db.models import Q

from servers.admin_audit.models import actor_snapshot

RIDER_PROMO, DRIVER_INCENTIVE, REFERRAL = 'rider_promo', 'driver_incentive', 'referral'
KIND_CHOICES = [(RIDER_PROMO, 'Rider promo'), (DRIVER_INCENTIVE, 'Driver incentive'),
                (REFERRAL, 'Referral')]
KINDS = tuple(k for k, _ in KIND_CHOICES)

DRAFT, PENDING, SCHEDULED, LIVE, PAUSED, EXHAUSTED, ENDED, CANCELLED = (
    'draft', 'pending_approval', 'scheduled', 'live', 'paused', 'exhausted', 'ended', 'cancelled')
STATE_CHOICES = [(s, s.replace('_', ' ').capitalize()) for s in (
    DRAFT, PENDING, SCHEDULED, LIVE, PAUSED, EXHAUSTED, ENDED, CANCELLED)]
#: States in which the campaign can still take a NEW application or progress.
SERVING = (LIVE,)
#: A campaign in one of these holds reservations that must still be honoured and settled.
TERMINAL = (ENDED, CANCELLED)


class Campaign(models.Model):
    kind = models.CharField(max_length=20, choices=KIND_CHOICES, db_index=True)
    name = models.CharField(max_length=120)
    description = models.CharField(max_length=256, blank=True, default='')
    state = models.CharField(max_length=20, choices=STATE_CHOICES, default=DRAFT, db_index=True)

    #: ISO country (`IN`, `US`) and the node the campaign is anchored at (a country or a city).
    country = models.CharField(max_length=2)
    geo_node = models.ForeignKey('admin_access.GeoNode', null=True, blank=True,
                                 on_delete=models.PROTECT, related_name='campaigns')
    currency = models.CharField(max_length=3)

    # --- the budget: integer minor units of `currency` -------------------------------------
    #: NULL only on a wrapped legacy row, which never had one (it keeps behaving as before).
    budget_minor = models.BigIntegerField(null=True, blank=True)
    committed_minor = models.BigIntegerField(default=0)
    spent_minor = models.BigIntegerField(default=0)
    #: The most `committed + spent` has ever stood above `budget`; 0 while it never did.
    overshoot_minor = models.BigIntegerField(default=0)
    #: True (the default): a new application is admitted only if its whole amount fits in what
    #: is left (a hard cap). False: it is admitted while ANY budget is left, so the last
    #: reservation may cross the cap by less than one discount (the overshoot is recorded).
    strict_cap = models.BooleanField(default=True)

    starts_at = models.DateTimeField(null=True, blank=True)
    ends_at = models.DateTimeField(null=True, blank=True)

    #: Kind-specific settings, validated by `workflow.clean_config`.
    config = models.JSONField(default=dict, blank=True)

    # --- links to what the campaign wraps / creates ----------------------------------------
    #: The `PromoCode` the booking path serves for a rider promo (created when the checker
    #: approves, or the existing row a legacy campaign wraps).
    promo_code = models.OneToOneField('ride.PromoCode', null=True, blank=True,
                                      on_delete=models.PROTECT, related_name='campaign')
    #: The `IncentiveQuest` a legacy incentive campaign wraps.
    quest = models.OneToOneField('driver.IncentiveQuest', null=True, blank=True,
                                 on_delete=models.PROTECT, related_name='campaign')
    #: True for a campaign that WRAPS a row that existed before this module (no budget, no
    #: checker, behaves as it always did). A new campaign is never legacy.
    is_legacy = models.BooleanField(default=False)

    reason = models.TextField(blank=True, default='')
    approval_request_id = models.BigIntegerField(null=True, blank=True)
    budget_request_id = models.BigIntegerField(null=True, blank=True)

    created_by_id = models.BigIntegerField(null=True, blank=True)
    created_by_label = models.CharField(max_length=255, blank=True, default='')
    created_by_email = models.CharField(max_length=254, blank=True, default='')
    created_by_role = models.CharField(max_length=64, blank=True, default='')

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    launched_at = models.DateTimeField(null=True, blank=True)
    paused_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    exhausted_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at', '-id']
        constraints = [
            models.CheckConstraint(condition=Q(committed_minor__gte=0), name='campaign_committed_nonneg'),
            models.CheckConstraint(condition=Q(spent_minor__gte=0), name='campaign_spent_nonneg'),
            models.CheckConstraint(condition=Q(budget_minor__isnull=True) | Q(budget_minor__gte=0),
                                   name='campaign_budget_nonneg'),
        ]
        indexes = [models.Index(fields=['kind', 'state'], name='campaign_kind_state_idx')]

    def __str__(self):
        return f'Campaign#{self.pk} {self.kind} {self.name} {self.state}'

    def set_created_by(self, user):
        snap = actor_snapshot(user)
        self.created_by_id = snap['actor_id']
        self.created_by_label = snap['actor_label']
        self.created_by_email = snap['actor_email']
        self.created_by_role = snap['actor_role']

    @property
    def remaining_minor(self):
        if self.budget_minor is None:
            return None
        return max(0, self.budget_minor - self.committed_minor - self.spent_minor)


# ---------------------------------------------------------------------------
# The platform-funded ledger of promotions
# ---------------------------------------------------------------------------

DISCOUNT_FUNDED = 'promo_discount_platform_funded'
INCENTIVE_REWARD = 'incentive_reward'
REFERRAL_REWARD = 'referral_reward'
ENTRY_KIND_CHOICES = [(DISCOUNT_FUNDED, 'Promo discount, platform funded'),
                      (INCENTIVE_REWARD, 'Driver incentive reward'),
                      (REFERRAL_REWARD, 'Referral reward')]


class LedgerEntry(models.Model):
    """One money fact of a campaign: what the platform paid for it.

    A promo discount is paid by the platform, not by the driver: the driver's earnings and
    commission are computed on the FULL fare, the rider pays less, and this row records the
    difference. For a CASH trip the driver collected less than the fare, so the same amount is
    also credited to the driver's wallet (`wallet_transaction`); for an online trip the
    platform simply collects less from the rider and no driver balance moves.
    An incentive or driver referral reward is also a credit to the driver's wallet (paid out
    with the next weekly payout). A rider referral reward is a rider wallet credit.
    """

    kind = models.CharField(max_length=40, choices=ENTRY_KIND_CHOICES, db_index=True)
    campaign = models.ForeignKey(Campaign, on_delete=models.PROTECT, related_name='ledger')
    #: The person the money is for (the rider whose fare was discounted, the driver paid).
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                             related_name='promotion_ledger')
    trip = models.ForeignKey('ride.Trip', null=True, blank=True, on_delete=models.SET_NULL,
                             related_name='promotion_ledger')
    amount_minor = models.BigIntegerField()
    currency = models.CharField(max_length=3, db_index=True)
    wallet_transaction = models.ForeignKey('rider.WalletTransaction', null=True, blank=True,
                                           on_delete=models.PROTECT, related_name='+')
    idempotency_key = models.CharField(max_length=100, unique=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at', '-id']
        constraints = [models.CheckConstraint(condition=Q(amount_minor__gt=0),
                                              name='promoledger_amount_positive')]

    def __str__(self):
        return f'{self.kind} {self.amount_minor} {self.currency} campaign={self.campaign_id}'


# ---------------------------------------------------------------------------
# Driver incentive progress
# ---------------------------------------------------------------------------

IN_PROGRESS, COMPLETED, UNFUNDED = 'in_progress', 'completed', 'unfunded'
PROGRESS_STATUS_CHOICES = [(IN_PROGRESS, 'In progress'), (COMPLETED, 'Completed, reward paid'),
                           (UNFUNDED, 'Completed, budget used up')]


class IncentiveProgress(models.Model):
    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name='progress')
    driver = models.ForeignKey('driver.Driver', on_delete=models.CASCADE,
                               related_name='incentive_progress')
    trips_counted = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=12, choices=PROGRESS_STATUS_CHOICES, default=IN_PROGRESS)
    reward_minor = models.BigIntegerField(default=0)
    currency = models.CharField(max_length=3)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-trips_counted', 'id']
        constraints = [models.UniqueConstraint(fields=['campaign', 'driver'],
                                               name='incentive_progress_one_per_driver')]


class IncentiveTrip(models.Model):
    """A trip that counted toward a driver's incentive. UNIQUE per (campaign, trip), which is what
    makes progress idempotent: a replayed completion counts nothing twice."""

    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name='counted_trips')
    driver = models.ForeignKey('driver.Driver', on_delete=models.CASCADE, related_name='+')
    trip = models.ForeignKey('ride.Trip', on_delete=models.CASCADE, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['campaign', 'trip'],
                                               name='incentive_trip_once')]


# ---------------------------------------------------------------------------
# Referrals
# ---------------------------------------------------------------------------

class ReferralCode(models.Model):
    """A user's shareable code. One per user."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                related_name='referral_code')
    code = models.CharField(max_length=16, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)


ATTACHED, QUALIFIED, REWARDED, BLOCKED = 'attached', 'qualified', 'rewarded', 'blocked'
REFERRAL_STATUS_CHOICES = [(ATTACHED, 'Attached, trips pending'), (QUALIFIED, 'Qualified'),
                           (REWARDED, 'Rewarded'), (BLOCKED, 'Reward blocked')]


class Referral(models.Model):
    """One referee attached to one referrer under one campaign."""

    campaign = models.ForeignKey(Campaign, on_delete=models.PROTECT, related_name='referrals')
    referrer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                 related_name='referrals_made')
    referee = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                related_name='referral_received')
    code_used = models.CharField(max_length=16)
    status = models.CharField(max_length=12, choices=REFERRAL_STATUS_CHOICES, default=ATTACHED,
                              db_index=True)
    #: A salted hash of the device id the app sent when the referee signed up (never the id).
    device_hash = models.CharField(max_length=64, blank=True, default='')
    trips_completed = models.PositiveIntegerField(default=0)
    #: Why the reward was blocked: `same_device`, `same_phone`, `same_instrument` (a list).
    blocked_signals = models.JSONField(default=list, blank=True)
    referrer_reward_minor = models.BigIntegerField(default=0)
    referee_reward_minor = models.BigIntegerField(default=0)
    currency = models.CharField(max_length=3)
    rewarded_at = models.DateTimeField(null=True, blank=True)
    blocked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at', '-id']
        constraints = [
            # A person can be referred once, ever.
            models.UniqueConstraint(fields=['referee'], name='referral_referee_once'),
            models.CheckConstraint(condition=~Q(referrer=models.F('referee')),
                                   name='referral_not_self'),
        ]


class ReferralTrip(models.Model):
    """A referee trip that counted toward the threshold; unique per (referral, trip)."""

    referral = models.ForeignKey(Referral, on_delete=models.CASCADE, related_name='counted_trips')
    trip = models.ForeignKey('ride.Trip', on_delete=models.CASCADE, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['referral', 'trip'],
                                               name='referral_trip_once')]
