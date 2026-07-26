from django.db import models
from django.contrib.auth import get_user_model

User=get_user_model()


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

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user_id', '-created_at'], name='ticket_user_recent_idx'),
            models.Index(fields=['status', '-created_at'], name='ticket_status_recent_idx'),
        ]

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
    ]
    ticket=models.ForeignKey(SupportTicket,on_delete=models.CASCADE,related_name='messages')
    author=models.ForeignKey(User,on_delete=models.SET_NULL,null=True,blank=True,
                             related_name='support_messages_sent')
    author_role=models.CharField(max_length=16,choices=AUTHOR_CHOICES,default='user')
    body=models.TextField()
    created_at=models.DateTimeField(auto_now_add=True,db_index=True)

    class Meta:
        ordering = ['created_at']
        indexes = [
            models.Index(fields=['ticket', 'created_at'], name='msg_ticket_time_idx'),
        ]

    def __str__(self):
        return f'Message ticket={self.ticket_id} role={self.author_role}'
