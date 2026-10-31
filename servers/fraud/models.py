"""Fraud & Risk, seed of module 13.

Module 13 does not exist yet. This is the smallest thing the Promotions module needs: a place
to put "this reward was blocked, and why" so a person can look at it. Module 13 extends it
(review actions, assignment, more producers); nothing here is a decision about how fraud is
adjudicated.

A `RiskFlag` names the SUBJECT (the user the evidence is about), a `kind`, the evidence as JSON
and a status. THE EVIDENCE CARRIES NO RAW PERSONAL DATA: user ids, the names of the signals that
matched (`same_device`, `same_phone`, `same_instrument`), campaign and trip ids, amounts in minor
units. Never a phone number, an email, a token or an account number; when two accounts were
compared by a fingerprint, the evidence says WHICH signal matched, not the value.
"""

from django.conf import settings
from django.db import models

#: Kinds raised so far. Module 13 adds to this list.
REFERRAL_ABUSE = 'referral_abuse'
KIND_CHOICES = [(REFERRAL_ABUSE, 'Referral abuse')]

OPEN, DISMISSED, CONFIRMED = 'open', 'dismissed', 'confirmed'
STATUS_CHOICES = [(OPEN, 'Open'), (DISMISSED, 'Dismissed'), (CONFIRMED, 'Confirmed')]


class RiskFlag(models.Model):
    subject = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                related_name='risk_flags')
    kind = models.CharField(max_length=40, choices=KIND_CHOICES, db_index=True)
    #: Signal names, ids and amounts. No raw personal data (see the module docstring).
    evidence = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=OPEN, db_index=True)
    #: Where the flag sits for scope: the subject's city (else their country). NULL is visible
    #: to unrestricted callers only.
    anchor_node = models.ForeignKey('admin_access.GeoNode', null=True, blank=True,
                                    on_delete=models.PROTECT, related_name='risk_flags')
    #: What raised it, so one event raises one flag: e.g. `referral:41`.
    source_ref = models.CharField(max_length=64, blank=True, default='', db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at', '-id']
        constraints = [
            models.UniqueConstraint(fields=['kind', 'source_ref'],
                                    condition=~models.Q(source_ref=''),
                                    name='riskflag_one_per_source'),
        ]

    def __str__(self):
        return f'RiskFlag#{self.pk} {self.kind} {self.status}'
