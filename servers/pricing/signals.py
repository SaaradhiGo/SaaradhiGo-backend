"""Invalidate the in-process polygon cache when a ServiceZone changes."""

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from servers.pricing.models import ServiceZone
from servers.pricing.services import invalidate_polygon_cache


@receiver(post_save, sender=ServiceZone)
def _zone_saved(sender, instance, **kwargs):
    invalidate_polygon_cache(instance.id)


@receiver(post_delete, sender=ServiceZone)
def _zone_deleted(sender, instance, **kwargs):
    invalidate_polygon_cache(instance.id)
