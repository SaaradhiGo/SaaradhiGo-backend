"""Trusted contacts, trip share links, SMS opt-outs and the contact SMS log (2026-11-09)."""

from django.conf import settings
from django.db import models


class TrustedContact(models.Model):
    """Someone a rider trusts to follow their trips. At most five per rider (services.MAX_CONTACTS)."""

    rider = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                              related_name='trusted_contacts')
    name = models.CharField(max_length=60)
    #: E.164, normalised for the rider's market.
    phone = models.CharField(max_length=20)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['rider', 'phone'],
                                               name='trusted_contact_unique_phone')]
        ordering = ['id']


class SharingPreference(models.Model):
    """"Share every trip automatically": off until the rider turns it on."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                related_name='sharing_preference')
    auto_share = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)


class TripShare(models.Model):
    """The one live link for a trip: /t/<token>.

    The token is 32 random bytes (256 bits, urlsafe). Stopping clears `active`; sharing
    again issues a NEW token, so a stopped link never comes back to life. The link also
    dies 30 minutes after the trip ends (services.share_is_live)."""

    trip = models.OneToOneField('ride.Trip', on_delete=models.CASCADE, related_name='share')
    token = models.CharField(max_length=64, unique=True)
    active = models.BooleanField(default=True)
    #: How it was started: manual (the rider), auto (trip start), sos.
    source = models.CharField(max_length=10, default='manual')
    created_at = models.DateTimeField(auto_now_add=True)
    stopped_at = models.DateTimeField(null=True, blank=True)


class SmsOptOut(models.Model):
    """A number that replied STOP. It gets no further contact messages."""

    phone = models.CharField(max_length=20, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)


class ContactSms(models.Model):
    """One message to a trusted contact: who, why and what happened. Never the text."""

    KINDS = [('added', 'Added as a contact'), ('trip_share', 'Trip shared'), ('sos', 'SOS')]
    rider = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                              related_name='+')
    contact = models.ForeignKey(TrustedContact, on_delete=models.SET_NULL, null=True,
                                related_name='messages')
    trip_id = models.BigIntegerField(null=True, blank=True)
    kind = models.CharField(max_length=12, choices=KINDS)
    #: queued (handed to the provider), sink (QA test sink: recorded, not sent),
    #: opted_out, rate_limited, failed.
    status = models.CharField(max_length=14)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
