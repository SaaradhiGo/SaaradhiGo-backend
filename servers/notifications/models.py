"""Notifications (module 11): templates, the outbox / delivery log, preferences, campaigns.

THE SHAPE
---------
    NotificationTemplate    copy for one (key, channel, country, locale), versioned
      TemplateRegistration  per-provider registration (India DLT ids, sender id, WhatsApp name)
    NotificationMessage     the outbox AND the delivery log: one row per recipient x channel
      NotificationStatusEvent  append-only status changes (dispatcher and signed webhooks)
    NotificationPreference  user x channel x category opt-in / opt-out, with source and time
    Campaign                a promotional broadcast; sending it is a maker-checker action

NOTHING HERE HOLDS A CREDENTIAL, AND NO MESSAGE ROW HOLDS A RAW CONTACT
-----------------------------------------------------------------------
A message stores the recipient's USER ID and a MASKED contact. The real phone, email or
push token is read from the user at send time, so a delivery log leaks nothing a log line
could not. A rendered body is kept only for templates with no personal variables (and only
while it is still needed otherwise); the hash is always kept.
"""

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

from servers.admin_audit.models import actor_snapshot

SMS, WHATSAPP, EMAIL, PUSH, IN_APP = 'sms', 'whatsapp', 'email', 'push', 'in_app'
CHANNELS = (SMS, WHATSAPP, EMAIL, PUSH, IN_APP)
CHANNEL_CHOICES = [(c, c) for c in CHANNELS]

TRANSACTIONAL, PROMOTIONAL = 'transactional', 'promotional'
CATEGORIES = (TRANSACTIONAL, PROMOTIONAL)
CATEGORY_CHOICES = [(c, c) for c in CATEGORIES]

DRAFT, PENDING_APPROVAL, ACTIVE, ARCHIVED = 'draft', 'pending_approval', 'active', 'archived'
TEMPLATE_STATUSES = [(s, s) for s in (DRAFT, PENDING_APPROVAL, ACTIVE, ARCHIVED)]

QUEUED, SENDING, SENT, DELIVERED, FAILED, SUPPRESSED = (
    'queued', 'sending', 'sent', 'delivered', 'failed', 'suppressed')
MESSAGE_STATUSES = [(s, s) for s in (QUEUED, SENDING, SENT, DELIVERED, FAILED, SUPPRESSED)]
#: Statuses after which the dispatcher never touches a row again.
TERMINAL = frozenset({SENT, DELIVERED, FAILED, SUPPRESSED})

#: Provider names that exist. `stub` records only; `in_app` writes the in-app row.
PROVIDERS = ('stub', 'msg91', 'twilio', 'ses', 'fcm', 'in_app')


def _snapshot_fields(prefix, user):
    snap = actor_snapshot(user)
    return {f'{prefix}_id': snap['actor_id'], f'{prefix}_label': snap['actor_label'],
            f'{prefix}_email': snap['actor_email'], f'{prefix}_role': snap['actor_role']}


class NotificationTemplate(models.Model):
    key = models.CharField(max_length=80, db_index=True)
    channel = models.CharField(max_length=10, choices=CHANNEL_CHOICES)
    #: The country node this copy is for; NULL = every country. A country-specific active
    #: template wins over an all-countries one.
    geo_node = models.ForeignKey(
        'admin_access.GeoNode', null=True, blank=True, on_delete=models.PROTECT,
        related_name='notification_templates')
    country = models.CharField(max_length=2, blank=True, default='')
    locale = models.CharField(max_length=8, default='en')
    category = models.CharField(max_length=14, choices=CATEGORY_CHOICES, default=TRANSACTIONAL)
    subject = models.CharField(max_length=200, blank=True, default='')   # email
    title = models.CharField(max_length=200, blank=True, default='')     # push, in-app
    body = models.TextField()
    #: Declared variable names. The body may use these and nothing else.
    variables = models.JSONField(default=list, blank=True)
    #: The subset of `variables` that carries personal data (a name, an address). A template
    #: with any of these never keeps its rendered body after the message is final.
    personal_variables = models.JSONField(default=list, blank=True)
    #: Sample values for the console preview. Never real people.
    sample_values = models.JSONField(default=dict, blank=True)
    version = models.PositiveIntegerField(default=1)
    status = models.CharField(max_length=18, choices=TEMPLATE_STATUSES, default=DRAFT, db_index=True)
    reason = models.TextField(blank=True, default='')
    created_by_id = models.BigIntegerField(null=True, blank=True)
    created_by_label = models.CharField(max_length=255, blank=True, default='')
    created_by_email = models.CharField(max_length=254, blank=True, default='')
    created_by_role = models.CharField(max_length=64, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    activated_at = models.DateTimeField(null=True, blank=True)
    archived_at = models.DateTimeField(null=True, blank=True)
    #: The ApprovalRequest that activated (or archived) this version.
    changed_via_id = models.BigIntegerField(null=True, blank=True)

    class Meta:
        ordering = ['key', 'channel', 'country', '-version']
        constraints = [
            models.UniqueConstraint(
                fields=['key', 'channel', 'country', 'locale', 'version'],
                name='notif_template_version_unique'),
            # One live copy per (key, channel, country, locale).
            models.UniqueConstraint(
                fields=['key', 'channel', 'country', 'locale'], condition=Q(status='active'),
                name='notif_template_one_active'),
        ]
        indexes = [models.Index(fields=['key', 'channel', 'status'], name='notif_tpl_lookup_idx')]

    def __str__(self):
        return f'{self.key}/{self.channel}/{self.country or "*"} v{self.version} {self.status}'

    @property
    def has_personal_variables(self):
        return bool(self.personal_variables)

    def set_created_by(self, user):
        for name, value in _snapshot_fields('created_by', user).items():
            setattr(self, name, value)


class TemplateRegistration(models.Model):
    """What a provider needs to carry this template (India DLT, WhatsApp, sender id)."""

    template = models.ForeignKey(NotificationTemplate, on_delete=models.CASCADE,
                                 related_name='registrations')
    provider = models.CharField(max_length=10)
    sender_id = models.CharField(max_length=32, blank=True, default='')
    #: India DLT template id (the id the telecom operator's registry gave the approved text).
    dlt_template_id = models.CharField(max_length=64, blank=True, default='')
    #: The DLT principal-entity id, when it differs from the account default.
    entity_id = models.CharField(max_length=64, blank=True, default='')
    #: The text exactly as DLT approved it, with `{#var#}` slots.
    registered_text = models.TextField(blank=True, default='')
    #: The provider's own template / flow id (MSG91 flow id, Twilio content sid).
    provider_template_id = models.CharField(max_length=128, blank=True, default='')
    whatsapp_template_name = models.CharField(max_length=128, blank=True, default='')
    whatsapp_namespace = models.CharField(max_length=128, blank=True, default='')

    class Meta:
        ordering = ['template_id', 'provider']
        constraints = [models.UniqueConstraint(fields=['template', 'provider'],
                                               name='notif_registration_one_per_provider')]

    def __str__(self):
        return f'registration {self.provider} for template {self.template_id}'


class NotificationMessage(models.Model):
    """One row per recipient x channel: the outbox and, afterwards, the delivery log."""

    template = models.ForeignKey(NotificationTemplate, null=True, blank=True,
                                 on_delete=models.PROTECT, related_name='messages')
    template_key = models.CharField(max_length=80, db_index=True)
    template_version = models.PositiveIntegerField(default=0)
    channel = models.CharField(max_length=10, choices=CHANNEL_CHOICES, db_index=True)
    category = models.CharField(max_length=14, choices=CATEGORY_CHOICES, default=TRANSACTIONAL)
    country = models.CharField(max_length=2, blank=True, default='')
    #: Where the recipient is, for the operator scope: a driver's home city, a rider's home
    #: or last-trip city, else their country node. NULL = unplaced (Global only).
    city = models.ForeignKey('admin_access.GeoNode', null=True, blank=True,
                             on_delete=models.PROTECT, related_name='+')
    recipient_user_id = models.BigIntegerField(db_index=True)
    recipient_masked = models.CharField(max_length=120, blank=True, default='')
    provider = models.CharField(max_length=10, blank=True, default='')
    provider_message_id = models.CharField(max_length=200, blank=True, default='', db_index=True)
    status = models.CharField(max_length=10, choices=MESSAGE_STATUSES, default=QUEUED, db_index=True)
    suppression_reason = models.CharField(max_length=200, blank=True, default='')
    attempts = models.PositiveSmallIntegerField(default=0)
    last_error = models.CharField(max_length=500, blank=True, default='')
    #: The system event that caused it (`commission.increase_notice`) or `campaign:<id>`.
    source = models.CharField(max_length=120, blank=True, default='')
    campaign = models.ForeignKey('notifications.Campaign', null=True, blank=True,
                                 on_delete=models.SET_NULL, related_name='messages')
    idempotency_key = models.CharField(max_length=200, unique=True)
    body_hash = models.CharField(max_length=64, blank=True, default='')
    #: The rendered text. Kept for templates with no personal variables; for the others it
    #: lives only while the message is not final (the send needs it) and is then cleared.
    subject = models.CharField(max_length=200, blank=True, default='')
    title = models.CharField(max_length=200, blank=True, default='')
    body = models.TextField(blank=True, default='')
    #: Ordered values for providers that fill registered-template slots (India SMS, WhatsApp).
    variable_values = models.JSONField(default=list, blank=True)
    #: Do not send before this (promotional quiet hours, retry backoff).
    not_before = models.DateTimeField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at', '-id']
        indexes = [
            models.Index(fields=['status', 'not_before'], name='notif_msg_due_idx'),
            models.Index(fields=['provider', 'provider_message_id'], name='notif_msg_provider_idx'),
            models.Index(fields=['provider', 'channel', 'created_at'], name='notif_msg_health_idx'),
        ]

    def __str__(self):
        return f'Message#{self.pk} {self.template_key}/{self.channel} {self.status}'


class NotificationStatusEvent(models.Model):
    """Append-only: every status change, from the dispatcher or a signed webhook."""

    message = models.ForeignKey(NotificationMessage, on_delete=models.CASCADE,
                                related_name='events')
    status = models.CharField(max_length=10, choices=MESSAGE_STATUSES)
    provider = models.CharField(max_length=10, blank=True, default='')
    provider_message_id = models.CharField(max_length=200, blank=True, default='')
    #: `dispatcher`, `webhook:<provider>`, `system`.
    source = models.CharField(max_length=40, default='dispatcher')
    detail = models.CharField(max_length=500, blank=True, default='')
    at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['at', 'id']
        constraints = [
            # A provider that repeats a callback changes nothing the second time.
            models.UniqueConstraint(
                fields=['provider', 'provider_message_id', 'status'],
                condition=~Q(provider_message_id=''),
                name='notif_event_idempotent_per_provider_id_status'),
        ]

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise ValueError('NotificationStatusEvent is append-only.')
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError('NotificationStatusEvent is append-only.')


class NotificationPreference(models.Model):
    """User x channel x category. No row means the default (see `preferences.default_for`)."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                             related_name='channel_preferences')
    channel = models.CharField(max_length=10, choices=CHANNEL_CHOICES)
    category = models.CharField(max_length=14, choices=CATEGORY_CHOICES)
    opted_in = models.BooleanField()
    #: `app`, `admin`, `default`, `webhook:twilio` (STOP), `webhook:ses` (bounce / complaint).
    source = models.CharField(max_length=40, default='app')
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['user', 'channel', 'category'],
                                               name='notif_pref_one_per_user_channel_category')]

    def __str__(self):
        return f'pref user={self.user_id} {self.channel}/{self.category}={self.opted_in}'


CAMPAIGN_DRAFT, CAMPAIGN_PENDING, CAMPAIGN_SCHEDULED, CAMPAIGN_SENDING, CAMPAIGN_SENT, \
    CAMPAIGN_CANCELLED = 'draft', 'pending_approval', 'scheduled', 'sending', 'sent', 'cancelled'
CAMPAIGN_STATUSES = [(s, s) for s in (CAMPAIGN_DRAFT, CAMPAIGN_PENDING, CAMPAIGN_SCHEDULED,
                                     CAMPAIGN_SENDING, CAMPAIGN_SENT, CAMPAIGN_CANCELLED)]


class Campaign(models.Model):
    name = models.CharField(max_length=120)
    country = models.CharField(max_length=2)
    template = models.ForeignKey(NotificationTemplate, on_delete=models.PROTECT,
                                 related_name='campaigns')
    #: {role: drivers|riders, cities: [codes], status: [...], last_active_days: int|null}
    audience = models.JSONField(default=dict)
    #: Values for the promotional template's variables (the same for every recipient).
    template_values = models.JSONField(default=dict, blank=True)
    #: The scope anchor: the single city the audience names, else the country node. A
    #: caller sees the campaign when their scope covers it.
    geo_node = models.ForeignKey('admin_access.GeoNode', null=True, blank=True,
                                 on_delete=models.PROTECT, related_name='notification_campaigns')
    scheduled_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=18, choices=CAMPAIGN_STATUSES, default=CAMPAIGN_DRAFT,
                              db_index=True)
    reason = models.TextField(blank=True, default='')
    approval_request_id = models.BigIntegerField(null=True, blank=True)
    #: The audience as counted when it was submitted, and as expanded when it was sent.
    audience_count = models.PositiveIntegerField(default=0)
    queued_count = models.PositiveIntegerField(default=0)
    suppressed_count = models.PositiveIntegerField(default=0)
    created_by_id = models.BigIntegerField(null=True, blank=True)
    created_by_label = models.CharField(max_length=255, blank=True, default='')
    created_by_email = models.CharField(max_length=254, blank=True, default='')
    created_by_role = models.CharField(max_length=64, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at', '-id']

    def __str__(self):
        return f'Campaign#{self.pk} {self.name} {self.status}'

    def set_created_by(self, user):
        for name, value in _snapshot_fields('created_by', user).items():
            setattr(self, name, value)
