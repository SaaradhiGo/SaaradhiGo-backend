from django.db import models
from django.contrib.auth import get_user_model

User=get_user_model()


class UserVisibleMessages(models.Manager):
    def get_queryset(self):
        return super().get_queryset().exclude(author_role='internal')


class SupportTicket(models.Model):
    STATUS_CHOICES = [
        ('OPEN', 'Open'),
        ('IN_PROGRESS', 'In progress'),
        ('WAITING_USER', 'Waiting on user'),
        ('CLOSED', 'Closed'),
    ]
    ISSUE_CHOICES = [
        ('payment', 'Payment issue'),
        ('fare_dispute', 'Fare dispute'),
        ('driver_behaviour', 'Driver behaviour'),
        ('rider_behaviour', 'Rider behaviour'),
        ('lost_item', 'Lost item'),
        ('safety', 'Safety concern'),
        ('account', 'Account / KYC'),
        ('app_bug', 'App bug'),
        ('other', 'Other'),
    ]
    PRIORITY_CHOICES = [
        ('urgent', 'Urgent'),
        ('high', 'High'),
        ('normal', 'Normal'),
        ('low', 'Low'),
    ]
    user_id=models.ForeignKey(User,on_delete=models.CASCADE,related_name='support_tickets')
    trip_id=models.ForeignKey('ride.Trip',on_delete=models.CASCADE,related_name='support_tickets',blank=True,null=True)
    issue_type=models.CharField(max_length=100,choices=ISSUE_CHOICES,default='other')
    status=models.CharField(max_length=20,choices=STATUS_CHOICES,default='OPEN',db_index=True)
    description=models.TextField(blank=True,null=True)
    assigned_to=models.ForeignKey(
        User,on_delete=models.SET_NULL,null=True,blank=True,
        related_name='assigned_tickets',
    )
    created_at=models.DateTimeField(auto_now_add=True,db_index=True)
    updated_at=models.DateTimeField(auto_now=True)
    resolved_at=models.DateTimeField(blank=True,null=True)

    # --- Support module (console module 5): priority and SLA ---------------
    priority=models.CharField(max_length=8,choices=PRIORITY_CHOICES,default='normal')
    # ISO country the ticket belongs to ('IN', 'US', or '' when it cannot be
    # placed), derived once at creation from the anchor; see `sla.derive_country`.
    country=models.CharField(max_length=2,blank=True,default='',db_index=True)
    first_response_due_at=models.DateTimeField(blank=True,null=True)
    resolution_due_at=models.DateTimeField(blank=True,null=True)
    first_responded_at=models.DateTimeField(blank=True,null=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user_id', '-created_at'], name='ticket_user_recent_idx'),
            models.Index(fields=['status', '-created_at'], name='ticket_status_recent_idx'),
            models.Index(fields=['country', 'status'], name='ticket_country_status_idx'),
        ]

    def save(self, *args, **kwargs):
        adding = self._state.adding
        if adding:
            # A safety concern is urgent from the first second.
            if self.issue_type == 'safety' and self.priority == 'normal':
                self.priority = 'urgent'
            if not self.country:
                from servers.support.sla import derive_country
                self.country = derive_country(self)
        super().save(*args, **kwargs)
        if adding and self.first_response_due_at is None:
            # After the insert so `created_at` (set by the database layer) is
            # exactly the base the due times count from.
            from servers.support.sla import apply_due_times
            apply_due_times(self)

    def __str__(self):
        return f'Ticket {self.id} - {self.issue_type}'


class SupportMessage(models.Model):
    """A reply on a SupportTicket -- from the user or from support staff.

    Lightweight thread model so the rider/driver and support can have
    a back-and-forth without leaving the app. Files / images are
    deliberately out of scope for Phase-0 to keep the surface small;
    a later phase adds attachments.
    """
    AUTHOR_CHOICES = [
        ('user', 'User'),
        ('support', 'Support'),
        ('system', 'System'),
        # An operator-only note. NEVER shown to the rider or driver: the
        # default manager below leaves these out, so `ticket.messages`, and
        # with it every user-facing serializer, cannot return one. Operator
        # code that needs them says so by name (`SupportMessage.everything`).
        ('internal', 'Internal note'),
    ]
    ticket=models.ForeignKey(SupportTicket,on_delete=models.CASCADE,related_name='messages')
    author=models.ForeignKey(User,on_delete=models.SET_NULL,null=True,blank=True,
                             related_name='support_messages_sent')
    author_role=models.CharField(max_length=16,choices=AUTHOR_CHOICES,default='user')
    body=models.TextField()
    created_at=models.DateTimeField(auto_now_add=True,db_index=True)
    # Makes an operator write replayable: the same key answers with the
    # message already written.
    idempotency_key=models.CharField(max_length=160,null=True,blank=True,unique=True)

    #: Fail closed: this is the manager every reverse relation (`ticket.messages`),
    #: prefetch and user-facing query uses, and it never returns an internal note.
    objects = UserVisibleMessages()
    #: Everything, for the operator console only.
    everything = models.Manager()

    class Meta:
        ordering = ['created_at']
        default_manager_name = 'objects'
        indexes = [
            models.Index(fields=['ticket', 'created_at'], name='msg_ticket_time_idx'),
        ]

    def __str__(self):
        return f'Message ticket={self.ticket_id} role={self.author_role}'


class SupportSlaPolicy(models.Model):
    """Minutes allowed for the first reply and for resolution, per country
    and priority. Changed only by `support.sla` holders, audited."""
    country=models.CharField(max_length=2)
    priority=models.CharField(max_length=8,choices=SupportTicket.PRIORITY_CHOICES)
    first_response_minutes=models.PositiveIntegerField()
    resolution_minutes=models.PositiveIntegerField()
    updated_at=models.DateTimeField(auto_now=True)
    updated_by=models.ForeignKey(User,on_delete=models.SET_NULL,null=True,blank=True,
                                 related_name='+')

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['country', 'priority'], name='sla_country_priority_uniq'),
        ]
        ordering = ['country', 'id']

    def __str__(self):
        return f'SLA {self.country}/{self.priority}'


class SupportMacro(models.Model):
    """A saved reply. Placeholders are filled server-side from a fixed
    allow-list (`macros.ALLOWED_PLACEHOLDERS`); anything else is refused at save."""
    LOCALE_CHOICES = [('en', 'English'), ('hi', 'Hindi'), ('te', 'Telugu')]
    title=models.CharField(max_length=120)
    body=models.TextField()
    locale=models.CharField(max_length=2,choices=LOCALE_CHOICES,default='en')
    # Null means every country.
    country=models.CharField(max_length=2,null=True,blank=True)
    # Empty list means every issue type.
    issue_types=models.JSONField(default=list,blank=True)
    active=models.BooleanField(default=True)
    created_by=models.ForeignKey(User,on_delete=models.SET_NULL,null=True,blank=True,
                                 related_name='+')
    created_at=models.DateTimeField(auto_now_add=True)
    updated_at=models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['title', 'id']

    def __str__(self):
        return f'Macro {self.id} {self.title}'
