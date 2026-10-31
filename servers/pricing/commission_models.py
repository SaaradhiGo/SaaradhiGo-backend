"""The layered, versioned commission model (AUDIT 27.36, module 8).

ONE SOURCE
----------
Before this module the commission lived in four places that could disagree:
`RateCard.commission_percent` (default 18), the `PLATFORM_COMMISSION_PERCENT`
PlatformSettings key, `settings.PLATFORM_COMMISSION_PERCENT` and a literal 18
in `commission_percent_for_trip`. Those four are now DEPRECATED and nothing
reads them (`tests/test_pricing_module.py` greps the source tree to keep it so).
`CommissionRule` is the only source.

LAYERS
------
A rule is anchored at a country, optionally narrowed to a city
(`geo_node.kind == 'city'`) and/or a vehicle class. The most specific ACTIVE
rule wins:

    country default            specificity 0
    city override              specificity 1
    vehicle-class override     specificity 2   (country-wide)
    city + vehicle class       specificity 3

Vehicle class is the deepest layer in the owner's wording ("country default,
then an optional city override, then an optional vehicle-class override").

LIFECYCLE
---------
draft -> pending_approval -> active -> archived. A rule is a VERSION of its
scope key (country, geo_node, vehicle_type): activating a new version closes the
previous one (`effective_to`), it never edits it. Which rule governs a moment is
decided by the effective window, not the status: a superseded rule stays in force
until the new one's `effective_from`.

NEVER RETROACTIVE
-----------------
A trip snapshots the percent, rule id and rule version at booking
(`Trip.commission_percent_snapshot` ...) and settlement reads the snapshot. A trip
that predates this module has no snapshot; it resolves the rule effective at its
`requested_at`, which is the same meaning the old `rate_card_for_trip(at=requested_at)`
had.

KIND
----
`kind` is `percentage | flat_fee | subscription`. Only percentage is implemented;
the others are refused at the door ("not supported yet"). `params` is the
extension point (a flat fee's amount, a plan id) so adding one later is code, not
a schema change.
"""

from decimal import Decimal

from django.db import models
from django.utils import timezone

PERCENTAGE = 'percentage'
FLAT_FEE = 'flat_fee'
SUBSCRIPTION = 'subscription'
KIND_CHOICES = [
    (PERCENTAGE, 'Percentage of the fare'),
    (FLAT_FEE, 'Flat fee per trip'),
    (SUBSCRIPTION, 'Driver subscription'),
]
IMPLEMENTED_KINDS = (PERCENTAGE,)

DRAFT = 'draft'
PENDING_APPROVAL = 'pending_approval'
ACTIVE = 'active'
ARCHIVED = 'archived'
STATUS_CHOICES = [
    (DRAFT, 'Draft'),
    (PENDING_APPROVAL, 'Pending approval'),
    (ACTIVE, 'Active'),
    (ARCHIVED, 'Archived'),
]

#: Applicable to a trip: approved at some point (active, or archived because a
#: later version took over). Whether it applies at a MOMENT is the window.
APPLICABLE_STATUSES = (ACTIVE, ARCHIVED)


class CommissionCountryConfig(models.Model):
    """Per-country rules around commission: notice period and the legal cap.

    `max_percent` is NULL until legal confirms the aggregator commission cap for
    the country (go-live blocker #13). TODO(legal): India (central and state) and
    the US launch state. With no cap set the console says "cap not confirmed
    (legal)" and activation is allowed; with one set, a rule above it is refused.
    A change here is a maker-checker action (`pricing.change`), like a rule.
    """

    country = models.CharField(max_length=2, unique=True)
    notice_days = models.PositiveSmallIntegerField(default=7)
    max_percent = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    version = models.PositiveIntegerField(default=1)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by_label = models.CharField(max_length=255, blank=True, default='')
    reason = models.TextField(blank=True, default='')

    def __str__(self):
        return f'Commission config {self.country} v{self.version}'


class CommissionRule(models.Model):
    """One version of a commission rule for one scope key. See the module docstring."""

    country = models.CharField(max_length=2, db_index=True)
    #: The node the rule is anchored at: the country node, or a city node for a
    #: city override. Null only for a country with no GeoNode (Global-only).
    geo_node = models.ForeignKey(
        'admin_access.GeoNode', null=True, blank=True, on_delete=models.PROTECT,
        related_name='commission_rules')
    vehicle_type = models.ForeignKey(
        'driver.VehicleType', null=True, blank=True, on_delete=models.PROTECT,
        related_name='commission_rules')

    kind = models.CharField(max_length=16, choices=KIND_CHOICES, default=PERCENTAGE)
    #: The rate, for kind=percentage. Two decimals, 0 to 100.
    percent = models.DecimalField(max_digits=5, decimal_places=2)
    #: Room for a flat fee amount or a subscription plan without a migration.
    params = models.JSONField(default=dict, blank=True)

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=DRAFT, db_index=True)
    version = models.PositiveIntegerField(default=1)
    effective_from = models.DateTimeField(default=timezone.now)
    effective_to = models.DateTimeField(null=True, blank=True)
    reason = models.TextField(blank=True, default='')

    # Who made this version: a snapshot, like an audit row.
    created_by_id = models.BigIntegerField(null=True, blank=True)
    created_by_label = models.CharField(max_length=255, blank=True, default='')
    created_by_email = models.CharField(max_length=254, blank=True, default='')
    created_by_role = models.CharField(max_length=64, blank=True, default='')

    #: The maker-checker request that activated (or is deciding) this version.
    approval_request_id = models.BigIntegerField(null=True, blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    activated_at = models.DateTimeField(null=True, blank=True)
    #: True when the activation moved effective_from later to honour the notice period.
    effective_from_adjusted = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['country', 'geo_node_id', 'vehicle_type_id', '-version']
        indexes = [
            models.Index(fields=['country', 'status'], name='commrule_country_status_idx'),
            models.Index(fields=['status', 'effective_from'], name='commrule_status_from_idx'),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(percent__gte=0) & models.Q(percent__lte=100),
                name='commrule_percent_0_100'),
        ]

    def __str__(self):
        return f'CommissionRule {self.country}/{self.level} v{self.version} {self.percent}% {self.status}'

    @property
    def level(self):
        city = self.geo_node_id is not None and self.geo_node.kind == 'city'
        if city and self.vehicle_type_id:
            return 'city_vehicle'
        if city:
            return 'city'
        if self.vehicle_type_id:
            return 'vehicle'
        return 'country'

    @property
    def specificity(self):
        city = self.geo_node_id is not None and self.geo_node.kind == 'city'
        return (2 if self.vehicle_type_id else 0) + (1 if city else 0)

    def applies_at(self, at):
        if self.status not in APPLICABLE_STATUSES:
            return False
        if self.effective_from > at:
            return False
        return self.effective_to is None or self.effective_to > at


class DriverNotice(models.Model):
    """OUTBOX row: tell the drivers in scope that a commission increase is coming.

    The Notifications module (13) does not exist yet. Writing the row is real;
    SENDING is a stub (`servers.pricing.driver_notices.deliver`) that leaves the row
    `queued` and logs. When module 13 lands it drains `status='queued'` rows.
    """

    QUEUED = 'queued'
    SENT = 'sent'

    rule = models.ForeignKey(CommissionRule, on_delete=models.CASCADE, related_name='notices')
    country = models.CharField(max_length=2, db_index=True)
    geo_node = models.ForeignKey(
        'admin_access.GeoNode', null=True, blank=True, on_delete=models.PROTECT,
        related_name='driver_notices')
    vehicle_type = models.ForeignKey(
        'driver.VehicleType', null=True, blank=True, on_delete=models.PROTECT,
        related_name='driver_notices')
    old_percent = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    new_percent = models.DecimalField(max_digits=5, decimal_places=2)
    effective_from = models.DateTimeField()
    status = models.CharField(max_length=10, default=QUEUED, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at', '-id']
        constraints = [
            models.UniqueConstraint(fields=['rule'], name='drivernotice_one_per_rule'),
        ]

    def __str__(self):
        return f'DriverNotice rule={self.rule_id} {self.old_percent}->{self.new_percent} {self.status}'


ZERO = Decimal('0.00')
