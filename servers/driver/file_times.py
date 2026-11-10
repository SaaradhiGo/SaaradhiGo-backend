"""When each driver and vehicle file was last uploaded (10 Nov v2), for "Uploaded 10 Nov" on its tile.

Files arrive through several endpoints (profile, documents, vehicles, console), so the time is
stamped on save rather than in each view: a file column whose stored name changed gets `now`.
A save that names `update_fields` without a file column costs nothing.
"""
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver
from django.utils import timezone

from .models import Driver, Vehicle

DRIVER_COLUMNS = ('photo_pending', 'address_proof_doc', 'aadhaar_doc', 'pan_doc', 'police_verification_doc',
                  'license_doc', 'license_doc_back')
VEHICLE_COLUMNS = ('rc_doc', 'insurance_doc', 'permit_doc', 'fitness_doc', 'puc_doc', 'inspection_doc',
                   'vehicle_pic', 'photo_back', 'photo_left', 'photo_right', 'photo_interior')


def _name(instance, column):
    return getattr(getattr(instance, column, None), 'name', '') or ''


def _stamp(sender, instance, update_fields, columns):
    if update_fields is not None and not set(update_fields) & set(columns):
        return
    old = {}
    if instance.pk:
        old = sender.objects.filter(pk=instance.pk).values(*columns).first() or {}
    now = timezone.now().isoformat()
    times = dict(instance.file_times or {})
    changed = False
    for col in columns:
        name = _name(instance, col)
        if name and name != (old.get(col) or ''):
            times[col] = now
            changed = True
    if changed:
        instance.file_times = times
        # A save limited to `update_fields` would drop the new times: post_save writes them.
        instance._file_times_dirty = update_fields is not None and 'file_times' not in update_fields


@receiver(pre_save, sender=Driver, dispatch_uid='driver_file_times')
def _driver_files(sender, instance, update_fields=None, raw=False, **kwargs):
    if not raw:
        _stamp(sender, instance, update_fields, DRIVER_COLUMNS)


@receiver(pre_save, sender=Vehicle, dispatch_uid='vehicle_file_times')
def _vehicle_files(sender, instance, update_fields=None, raw=False, **kwargs):
    if not raw:
        _stamp(sender, instance, update_fields, VEHICLE_COLUMNS)


@receiver(post_save, sender=Driver, dispatch_uid='driver_file_times_write')
@receiver(post_save, sender=Vehicle, dispatch_uid='vehicle_file_times_write')
def _write(sender, instance, **kwargs):
    if getattr(instance, '_file_times_dirty', False):
        instance._file_times_dirty = False
        sender.objects.filter(pk=instance.pk).update(file_times=instance.file_times)


def uploaded_at(owner, column):
    return (getattr(owner, 'file_times', None) or {}).get(column) if owner is not None else None
