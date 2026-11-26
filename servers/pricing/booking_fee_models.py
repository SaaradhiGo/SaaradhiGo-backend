"""The rider booking fee per place and class (owner ruling, 26 Nov): the model. See booking_fees.py."""

from django.db import models
from django.utils import timezone

ACTIVE, ARCHIVED = 'active', 'archived'


class RiderBookingFee(models.Model):
    country = models.CharField(max_length=2, db_index=True)
    #: '' for the country; else the GeoNode code of the state, city or zone ('IN-TG-HYD').
    scope_code = models.CharField(max_length=32, blank=True, default='')
    currency = models.CharField(max_length=3, blank=True, default='')
    #: {"auto": 0, "suv": 500}: minor units per class. A class not named inherits from the level above.
    amounts = models.JSONField(default=dict, blank=True)
    version = models.PositiveIntegerField(default=1)
    status = models.CharField(max_length=10, default=ACTIVE, db_index=True)
    effective_from = models.DateField()
    effective_to = models.DateField(null=True, blank=True)
    approval_request_id = models.BigIntegerField(null=True, blank=True)
    created_by_label = models.CharField(max_length=255, blank=True, default='')
    reason = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        app_label = 'pricing'
        ordering = ['country', 'scope_code', '-version']
        constraints = [models.UniqueConstraint(fields=['country', 'scope_code', 'version'],
                                               name='booking_fee_version_unique')]

    def in_force_on(self, day):
        return self.effective_from <= day and not (self.effective_to and self.effective_to <= day)
