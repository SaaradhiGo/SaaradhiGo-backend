"""Safety module (operator console, module 6): incidents, signals, config.

WHAT AN INCIDENT IS
-------------------
An SOS is an alarm; an incident is the case file around it. Every SOS opens
an incident or joins the open one for its trip, so the people answering see
one thing per emergency, with a timeline, the people and trips involved, and
the actions taken. Incidents also open from a support ticket about safety,
from a detected signal (a long stop, an overrun) and by hand.

WHERE IT LIVES
--------------
`anchor_node` / `anchor_source` are resolved once when the incident opens
(`services.resolve_incident_anchor`): the trip's city, else the SOS anchor,
else a driver's home city, else the rider rule, else NOTHING. An incident
with no anchor is visible to EVERY scoped `safety.respond` holder, exactly
like an unanchored SOS (`scope.incidents_visible_to`).

NOTHING ASSUMES A COUNTRY
-------------------------
Timers, thresholds and emergency numbers are `SafetyCountryConfig` rows, one
active row per country, versioned. A country with no row has no timer, no
detection and "not configured" numbers: nothing falls back to another
country's values and nothing falls back to India.

APPEND-ONLY
-----------
`SafetyIncidentUpdate` rows are insert-only (the model refuses an update or
a delete), like `SOSEventUpdate`. `SafetyIncident` rows are never deleted.
"""

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

from servers.admin_audit.models import actor_snapshot

# --- incident vocabulary ----------------------------------------------------

CATEGORIES = [
    ('assault', 'Assault'),
    ('harassment', 'Harassment'),
    ('accident', 'Accident'),
    ('medical', 'Medical'),
    ('dangerous_driving', 'Dangerous driving'),
    ('route_deviation', 'Route deviation'),
    ('long_stop', 'Long stop'),
    ('eta_overrun', 'ETA overrun'),
    ('lost_contact', 'Lost contact'),
    ('other', 'Other'),
]
CRITICAL, HIGH, MEDIUM, LOW = 'critical', 'high', 'medium', 'low'
SEVERITIES = [(CRITICAL, 'Critical'), (HIGH, 'High'), (MEDIUM, 'Medium'), (LOW, 'Low')]
#: Lower is more urgent. Used for the queue's sort.
SEVERITY_RANK = {CRITICAL: 0, HIGH: 1, MEDIUM: 2, LOW: 3}

OPEN, INVESTIGATING, ACTION_TAKEN, CLOSED = 'open', 'investigating', 'action_taken', 'closed'
STATUSES = [
    (OPEN, 'Open'), (INVESTIGATING, 'Investigating'),
    (ACTION_TAKEN, 'Action taken'), (CLOSED, 'Closed'),
]
LIVE_STATUSES = (OPEN, INVESTIGATING, ACTION_TAKEN)

SRC_SOS, SRC_TICKET, SRC_SIGNAL, SRC_MANUAL = 'sos', 'support_ticket', 'signal', 'manual'
SOURCES = [
    (SRC_SOS, 'SOS'), (SRC_TICKET, 'Support ticket'),
    (SRC_SIGNAL, 'Detected signal'), (SRC_MANUAL, 'Manual'),
]

OUTCOMES = [
    ('no_action', 'No action needed'),
    ('warning', 'Warning issued'),
    ('driver_suspended', 'Driver suspended'),
    ('rider_blocked', 'Rider blocked'),
    ('police_referral', 'Referred to police'),
    ('other', 'Other'),
]

#: `anchor_source` values.
ANCHOR_TRIP, ANCHOR_SOS, ANCHOR_DRIVER_HOME = 'trip', 'sos', 'driver_home'
ANCHOR_RIDER, ANCHOR_RIDER_COUNTRY, ANCHOR_UNANCHORED = 'rider', 'rider_country', 'unanchored'
ANCHOR_SOURCES = [
    (ANCHOR_TRIP, "The trip's city"),
    (ANCHOR_SOS, "The SOS event's anchor"),
    (ANCHOR_DRIVER_HOME, "The driver's home city"),
    (ANCHOR_RIDER, "A city the rider rides in"),
    (ANCHOR_RIDER_COUNTRY, "The rider's country, from their phone"),
    (ANCHOR_UNANCHORED, 'No anchor: every safety responder sees it'),
]

# Link kinds. The ids are: Driver.pk, the rider's AUTH USER pk (the console's
# /riders/<id> key), Trip.pk, SupportTicket.pk, SOSEvent.pk.
LINK_DRIVER, LINK_RIDER, LINK_TRIP, LINK_TICKET, LINK_SOS = (
    'driver', 'rider', 'trip', 'ticket', 'sos')
LINK_KINDS = [
    (LINK_DRIVER, 'Driver'), (LINK_RIDER, 'Rider'), (LINK_TRIP, 'Trip'),
    (LINK_TICKET, 'Support ticket'), (LINK_SOS, 'SOS event'),
]

# Timeline kinds.
U_OPENED, U_NOTE, U_STATUS, U_ASSIGNED, U_LINK, U_UNLINK = (
    'opened', 'note', 'status', 'assigned', 'link', 'unlink')
U_ACTION, U_SOS, U_SIGNAL, U_ESCALATED, U_POLICE, U_CLOSED = (
    'action', 'sos', 'signal', 'escalated', 'police_referral', 'closed')
UPDATE_KINDS = [(k, k) for k in (
    U_OPENED, U_NOTE, U_STATUS, U_ASSIGNED, U_LINK, U_UNLINK, U_ACTION, U_SOS,
    U_SIGNAL, U_ESCALATED, U_POLICE, U_CLOSED)]

# --- signals ----------------------------------------------------------------

SIG_LONG_STOP, SIG_ETA_OVERRUN, SIG_ROUTE_DEVIATION = 'long_stop', 'eta_overrun', 'route_deviation'
SIGNAL_KINDS = [
    (SIG_LONG_STOP, 'Long stop'),
    (SIG_ETA_OVERRUN, 'ETA overrun'),
    (SIG_ROUTE_DEVIATION, 'Route deviation'),
]


# ---------------------------------------------------------------------------
# Per-country configuration, versioned
# ---------------------------------------------------------------------------

class SafetyCountryConfig(models.Model):
    """One version of a country's safety timers, thresholds and numbers.

    Shaped like `command_centre.AlertThreshold`: exactly one active row per
    country; an edit closes it (`valid_to`, `is_active=False`) and opens the
    next version, so "what was the line on the night" is a query. Every
    numeric field is nullable and NULL means "off" (no timer, no detection of
    that kind), never "use a default".
    """

    country = models.CharField(max_length=2, db_index=True)
    version = models.PositiveIntegerField(default=1)
    is_active = models.BooleanField(default=True)
    valid_from = models.DateTimeField(default=timezone.now, editable=False)
    valid_to = models.DateTimeField(null=True, blank=True)
    reason = models.TextField(blank=True, default='')

    # Timers (minutes).
    sos_ack_minutes = models.PositiveSmallIntegerField(null=True, blank=True)
    incident_assign_minutes = models.PositiveSmallIntegerField(null=True, blank=True)

    # long_stop: unchanged within `radius` metres for `minutes`, away from the
    # trip's pickup and drop (`stop_exempt_radius_m`). `incident_minutes` is
    # the stop length at which the signal opens an incident.
    long_stop_minutes = models.PositiveSmallIntegerField(null=True, blank=True)
    long_stop_radius_m = models.PositiveIntegerField(null=True, blank=True)
    stop_exempt_radius_m = models.PositiveIntegerField(null=True, blank=True)
    long_stop_incident_minutes = models.PositiveSmallIntegerField(null=True, blank=True)

    # eta_overrun: elapsed beyond the estimate by `percent` AND `minutes`.
    eta_overrun_percent = models.PositiveSmallIntegerField(null=True, blank=True)
    eta_overrun_minutes = models.PositiveSmallIntegerField(null=True, blank=True)
    eta_overrun_incident_percent = models.PositiveSmallIntegerField(null=True, blank=True)
    eta_overrun_incident_minutes = models.PositiveSmallIntegerField(null=True, blank=True)

    # route_deviation: STUBBED. No planned route is stored per trip, so there
    # is nothing to measure against; the numbers are kept so the console can
    # hold them for the day a route is stored.
    route_deviation_meters = models.PositiveIntegerField(null=True, blank=True)
    route_deviation_minutes = models.PositiveSmallIntegerField(null=True, blank=True)

    #: `[{"label": "unified", "number": "..."}]`. From the country registry at
    #: seed time; edited here, not in code.
    emergency_numbers = models.JSONField(default=list, blank=True)

    created_by_id = models.BigIntegerField(null=True, blank=True)
    created_by_label = models.CharField(max_length=255, blank=True, default='')
    created_by_email = models.CharField(max_length=254, blank=True, default='')
    created_by_role = models.CharField(max_length=64, blank=True, default='')

    #: The numeric fields an edit may set, in display order.
    NUMBER_FIELDS = (
        'sos_ack_minutes', 'incident_assign_minutes',
        'long_stop_minutes', 'long_stop_radius_m', 'stop_exempt_radius_m',
        'long_stop_incident_minutes',
        'eta_overrun_percent', 'eta_overrun_minutes',
        'eta_overrun_incident_percent', 'eta_overrun_incident_minutes',
        'route_deviation_meters', 'route_deviation_minutes',
    )

    class Meta:
        ordering = ['country', '-version']
        constraints = [
            models.UniqueConstraint(
                fields=['country'], condition=Q(is_active=True),
                name='safety_one_active_config',
            ),
            models.UniqueConstraint(
                fields=['country', 'version'], name='safety_config_version_unique',
            ),
        ]

    def __str__(self):
        return f'{self.country} v{self.version}'

    def set_created_by(self, user):
        snap = actor_snapshot(user)
        self.created_by_id = snap.get('actor_id')
        self.created_by_label = (snap.get('actor_label') or '')[:255]
        self.created_by_email = (snap.get('actor_email') or '')[:254]
        self.created_by_role = (snap.get('actor_role') or '')[:64]

    def values(self):
        return {name: getattr(self, name) for name in self.NUMBER_FIELDS}

    def as_dict(self):
        return {
            'id': self.id, 'country': self.country, 'version': self.version,
            'is_active': self.is_active,
            'valid_from': self.valid_from.isoformat() if self.valid_from else None,
            'valid_to': self.valid_to.isoformat() if self.valid_to else None,
            'reason': self.reason,
            **self.values(),
            'emergency_numbers': list(self.emergency_numbers or []),
            'created_by': {'id': self.created_by_id, 'label': self.created_by_label,
                           'email': self.created_by_email, 'role': self.created_by_role},
        }


# ---------------------------------------------------------------------------
# Incidents
# ---------------------------------------------------------------------------

class SafetyIncident(models.Model):
    category = models.CharField(max_length=20, choices=CATEGORIES, default='other', db_index=True)
    severity = models.CharField(max_length=10, choices=SEVERITIES, default=HIGH, db_index=True)
    status = models.CharField(max_length=14, choices=STATUSES, default=OPEN, db_index=True)
    source = models.CharField(max_length=14, choices=SOURCES, default=SRC_MANUAL)
    #: Short and free of personal data: "SOS (panic) on trip 12".
    title = models.CharField(max_length=200, blank=True, default='')

    #: The incident's primary trip. At most ONE non-closed incident per trip
    #: (the constraint below), which is what makes "an SOS joins the open
    #: incident for its trip" safe under concurrency.
    trip = models.ForeignKey(
        'ride.Trip', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='safety_incidents',
    )

    anchor_node = models.ForeignKey(
        'admin_access.GeoNode', null=True, blank=True, on_delete=models.PROTECT,
        related_name='safety_incidents',
    )
    anchor_source = models.CharField(max_length=14, choices=ANCHOR_SOURCES,
                                     default=ANCHOR_UNANCHORED, db_index=True)
    #: ISO country, '' when nothing places the incident.
    country = models.CharField(max_length=2, blank=True, default='', db_index=True)

    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='assigned_safety_incidents',
    )
    opened_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='opened_safety_incidents',
    )
    opened_at = models.DateTimeField(default=timezone.now, db_index=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    outcome = models.CharField(max_length=20, choices=OUTCOMES, blank=True, default='')
    closing_summary = models.TextField(blank=True, default='')
    police_referred_at = models.DateTimeField(null=True, blank=True)
    police_reference = models.CharField(max_length=120, blank=True, default='')

    #: Escalation markers: the sweep fires each at most once per incident.
    assign_escalated_at = models.DateTimeField(null=True, blank=True)

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-opened_at']
        indexes = [
            models.Index(fields=['status', '-opened_at'], name='safety_inc_status_idx'),
            models.Index(fields=['country', 'status'], name='safety_inc_country_idx'),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['trip'], condition=Q(trip__isnull=False) & ~Q(status='closed'),
                name='safety_one_open_incident_per_trip',
            ),
        ]

    def __str__(self):
        return f'Incident#{self.id} [{self.status}] {self.category}/{self.severity}'

    @property
    def is_closed(self):
        return self.status == CLOSED


class SafetyIncidentLink(models.Model):
    """A person, trip, ticket or SOS the incident is about.

    One table for the five kinds. The target ids are validated against their
    own tables, and visibility checked, when the link is made
    (`services.link_target`); a link never grants access to the target.
    """

    incident = models.ForeignKey(SafetyIncident, on_delete=models.CASCADE, related_name='links')
    kind = models.CharField(max_length=8, choices=LINK_KINDS)
    target_id = models.BigIntegerField()
    linked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='+',
    )
    linked_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['linked_at', 'id']
        constraints = [
            models.UniqueConstraint(fields=['incident', 'kind', 'target_id'],
                                    name='safety_link_unique'),
        ]
        indexes = [models.Index(fields=['kind', 'target_id'], name='safety_link_target_idx')]


class SafetyIncidentUpdate(models.Model):
    """The timeline. Insert-only."""

    incident = models.ForeignKey(SafetyIncident, on_delete=models.CASCADE, related_name='updates')
    kind = models.CharField(max_length=16, choices=UPDATE_KINDS)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='+',
    )
    #: Frozen label of the actor, or 'system' for the sweeps.
    actor_label = models.CharField(max_length=255, blank=True, default='')
    from_status = models.CharField(max_length=14, blank=True, default='')
    to_status = models.CharField(max_length=14, blank=True, default='')
    note = models.TextField(blank=True, default='')
    data = models.JSONField(default=dict, blank=True)
    #: Optional client key making a retried note a no-op.
    idempotency_key = models.CharField(max_length=120, blank=True, default='')
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ['created_at', 'id']
        constraints = [
            models.UniqueConstraint(
                fields=['incident', 'idempotency_key'], condition=~Q(idempotency_key=''),
                name='safety_update_idempotent',
            ),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise RuntimeError('SafetyIncidentUpdate rows are insert-only.')
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise RuntimeError('SafetyIncidentUpdate rows are insert-only.')


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------

class SafetySignal(models.Model):
    """A detected condition on an in-progress trip. Detection only.

    One row per (trip, kind): the sweep refreshes `measured` while the
    condition lasts, so the queue shows one line per problem, not one per
    minute. A signal above its country's incident line (`incident` set) was
    promoted to an incident and raised a Command Centre alert; the rest wait
    in the signals queue for review.
    """

    kind = models.CharField(max_length=16, choices=SIGNAL_KINDS, db_index=True)
    trip = models.ForeignKey('ride.Trip', on_delete=models.CASCADE, related_name='safety_signals')
    country = models.CharField(max_length=2, blank=True, default='', db_index=True)
    anchor_node = models.ForeignKey(
        'admin_access.GeoNode', null=True, blank=True, on_delete=models.PROTECT,
        related_name='safety_signals',
    )
    #: The measured values, e.g. `{"stopped_minutes": 12.4, "radius_m": 50}`.
    measured = models.JSONField(default=dict, blank=True)
    #: The `SafetyCountryConfig.version` the numbers were judged against.
    threshold_version = models.PositiveIntegerField(default=0)
    incident = models.ForeignKey(
        SafetyIncident, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='signals',
    )
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    updated_at = models.DateTimeField(default=timezone.now)
    dismissed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='+',
    )
    dismissed_at = models.DateTimeField(null=True, blank=True)
    dismiss_reason = models.TextField(blank=True, default='')

    class Meta:
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(fields=['trip', 'kind'], name='safety_signal_one_per_trip_kind'),
        ]

    def __str__(self):
        return f'Signal#{self.id} {self.kind} trip={self.trip_id}'

    @property
    def is_dismissed(self):
        return self.dismissed_at is not None
