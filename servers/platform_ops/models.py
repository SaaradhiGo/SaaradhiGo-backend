"""Platform Settings (module 15): feature flags, app version policy and maintenance notices.

Three small tables. Everything else in this module is code over tables that already exist
(`GeoNode.status`, `PlatformSettings`, `AdminAuditLog`, the approvals framework).

FEATURE FLAGS
-------------
`FeatureFlag` holds the default; `FeatureFlagOverride` holds a per-country or per-city value
(a node that is a country or a city). Resolution is city override, then country override, then
the default (`flags.resolve`). Every change appends a `FeatureFlagVersion` snapshot, so "what was
this flag on the 3rd" is answerable without the audit log.

APP VERSION POLICY
------------------
One row per (app, platform): the minimum supported version (below it the user must update),
the latest version (below it they may), the store URL and a message.

MAINTENANCE NOTICES
-------------------
A scoped, time-boxed message for one audience. It is a notice only: "bookings paused" is a city
pause (`readiness.set_city_status`), never a notice.
"""

import re

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

FLAG_KEY_RE = re.compile(r'^[a-z][a-z0-9_.]{2,79}$')

OWNER_AREAS = [
    ('platform', 'Platform'), ('rider', 'Rider app'), ('driver', 'Driver app'),
    ('payments', 'Payments'), ('safety', 'Safety'), ('pricing', 'Pricing'),
    ('support', 'Support'), ('notifications', 'Notifications'),
]


def validate_flag_key(value):
    if not FLAG_KEY_RE.match(value or ''):
        raise ValidationError('A flag key is lowercase letters, digits, "_" or "." (3 to 80 '
                              'characters, starting with a letter).')


class FeatureFlag(models.Model):
    key = models.CharField(max_length=80, unique=True, validators=[validate_flag_key])
    description = models.CharField(max_length=300)
    default_enabled = models.BooleanField(default=False)
    owner_area = models.CharField(max_length=20, choices=OWNER_AREAS, default='platform')
    #: A flag that can change what riders pay or drivers earn, or a safety behaviour, changes
    #: only through a checker (`platform.flag_change`).
    money_affecting = models.BooleanField(default=False)
    safety = models.BooleanField(default=False)
    is_archived = models.BooleanField(default=False)
    version = models.PositiveIntegerField(default=1)
    updated_by_id = models.BigIntegerField(null=True, blank=True)
    updated_by_label = models.CharField(max_length=255, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['key']

    def __str__(self):
        return self.key

    @property
    def needs_checker(self):
        return bool(self.money_affecting or self.safety)


class FeatureFlagOverride(models.Model):
    flag = models.ForeignKey(FeatureFlag, on_delete=models.CASCADE, related_name='overrides')
    #: A country or a city node. Anything else is refused by `flags.set_override`.
    node = models.ForeignKey('admin_access.GeoNode', on_delete=models.PROTECT,
                             related_name='flag_overrides')
    enabled = models.BooleanField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['flag', 'node'],
                                               name='platform_flag_override_unique')]
        ordering = ['flag__key', 'node__path']


class FeatureFlagVersion(models.Model):
    """The full state of a flag after one change. Append-only by convention (no code updates it)."""

    flag = models.ForeignKey(FeatureFlag, on_delete=models.CASCADE, related_name='versions')
    version = models.PositiveIntegerField()
    snapshot = models.JSONField(default=dict)
    changed_by_id = models.BigIntegerField(null=True, blank=True)
    changed_by_label = models.CharField(max_length=255, blank=True, default='')
    reason = models.TextField(blank=True, default='')
    approval_request_id = models.BigIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['flag', 'version'],
                                               name='platform_flag_version_unique')]
        ordering = ['flag__key', '-version']


RIDER, DRIVER = 'rider', 'driver'
APP_CHOICES = [(RIDER, 'Rider app'), (DRIVER, 'Driver app')]
ANDROID, IOS = 'android', 'ios'
PLATFORM_CHOICES = [(ANDROID, 'Android'), (IOS, 'iOS')]


class AppVersionPolicy(models.Model):
    app = models.CharField(max_length=10, choices=APP_CHOICES)
    platform = models.CharField(max_length=10, choices=PLATFORM_CHOICES)
    minimum_version = models.CharField(max_length=20, default='0.0.0')
    latest_version = models.CharField(max_length=20, default='0.0.0')
    store_url = models.URLField(max_length=300, blank=True, default='')
    message = models.CharField(max_length=300, blank=True, default='')
    version = models.PositiveIntegerField(default=1)
    updated_by_id = models.BigIntegerField(null=True, blank=True)
    updated_by_label = models.CharField(max_length=255, blank=True, default='')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['app', 'platform'],
                                               name='platform_app_version_unique')]
        ordering = ['app', 'platform']


INFO, WARNING = 'info', 'warning'
SEVERITY_CHOICES = [(INFO, 'Info'), (WARNING, 'Warning')]
AUDIENCE_CHOICES = [('rider', 'Rider app'), ('driver', 'Driver app'), ('console', 'Console')]
NOTICE_MESSAGE_MAX = 280


class MaintenanceNotice(models.Model):
    #: NULL = global; otherwise a country or city node.
    scope_node = models.ForeignKey('admin_access.GeoNode', null=True, blank=True,
                                   on_delete=models.PROTECT, related_name='maintenance_notices')
    audience = models.CharField(max_length=10, choices=AUDIENCE_CHOICES)
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    message = models.CharField(max_length=NOTICE_MESSAGE_MAX)
    severity = models.CharField(max_length=10, choices=SEVERITY_CHOICES, default=INFO)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    created_by_id = models.BigIntegerField(null=True, blank=True)
    created_by_label = models.CharField(max_length=255, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-starts_at', '-id']
        indexes = [models.Index(fields=['audience', 'starts_at', 'ends_at'],
                                name='platform_notice_window_idx')]

    def active_at(self, when=None):
        when = when or timezone.now()
        return self.cancelled_at is None and self.starts_at <= when < self.ends_at

    @property
    def state(self):
        now = timezone.now()
        if self.cancelled_at is not None:
            return 'cancelled'
        if now < self.starts_at:
            return 'scheduled'
        if now >= self.ends_at:
            return 'ended'
        return 'active'

