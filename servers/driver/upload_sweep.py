"""Object-storage housekeeping: unattached uploads and records whose file is gone.

UNATTACHED UPLOADS (24 hours)
-----------------------------
A client presigns a PUT, uploads, then submits the key to a resource endpoint.
An upload that is never submitted -- the app closed, the form abandoned --
stays in the bucket forever, and for the KYC bucket that is a driver's licence
nobody is responsible for. `sweep_unattached_uploads` deletes objects under the
upload prefixes that are older than `UNATTACHED_UPLOAD_MAX_AGE_HOURS` and that
no record references. It runs hourly (Celery beat). A bucket lifecycle rule
would do the age half but cannot know what is referenced; this does both, and
works the same on any S3-compatible store.

Only keys under the known upload prefixes (`base.media.UPLOAD_KINDS`) are ever
considered, and an object is deleted only when NO file field on any model
holds that name.

RECORDS WHOSE FILE IS GONE
--------------------------
`files_missing_from_storage` finds file fields that name an object the store
does not have -- the case after QA moved from the Railway bucket to R2
(2026-11-02) and the old objects were not migrated. `clear_missing` empties
those fields, which is how the apps already show "not uploaded": the driver
uploads again.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from botocore.exceptions import ClientError
from django.apps import apps
from django.conf import settings
from django.db import models
from django.utils import timezone

from base.media import UPLOAD_KINDS
from base.storage_backends import PrivateDocumentStorage, PublicMediaStorage

logger = logging.getLogger(__name__)

_OUR_STORAGES = (PublicMediaStorage, PrivateDocumentStorage)


def _storage_of(field):
    # Django instantiates a callable `storage=` when the field is built.
    return field.storage


def file_fields():
    """Every (model, FileField) stored in our object storage."""
    out = []
    for model in apps.get_models():
        for field in model._meta.get_fields():
            if isinstance(field, models.FileField) and isinstance(_storage_of(field), _OUR_STORAGES):
                out.append((model, field))
    return out


def is_referenced(key, fields=None):
    for model, field in fields if fields is not None else file_fields():
        if model._default_manager.filter(**{field.name: key}).exists():
            return True
    return False


def sweep_unattached_uploads(now=None, dry_run=False, limit=5000):
    """Delete unreferenced uploads older than the cut-off. Returns counts."""
    from integrations.storage.s3 import _bucket, build_s3_client

    now = now or timezone.now()
    cutoff = now - timedelta(hours=settings.UNATTACHED_UPLOAD_MAX_AGE_HOURS)
    fields = file_fields()
    seen = deleted = kept = 0
    for kind, spec in UPLOAD_KINDS.items():
        client, bucket = build_s3_client(kind), _bucket(kind)
        pages = client.get_paginator('list_objects_v2').paginate(
            Bucket=bucket, Prefix=f"{spec['prefix']}/",
        )
        for page in pages:
            for obj in page.get('Contents', []):
                if seen >= limit:
                    return {'seen': seen, 'deleted': deleted, 'kept': kept, 'truncated': True}
                seen += 1
                if obj['LastModified'] >= cutoff or is_referenced(obj['Key'], fields):
                    kept += 1
                    continue
                if not dry_run:
                    client.delete_object(Bucket=bucket, Key=obj['Key'])
                deleted += 1
    # Counts only: a key is enough to fetch a public object.
    logger.info('unattached_upload_sweep seen=%s deleted=%s kept=%s', seen, deleted, kept)
    return {'seen': seen, 'deleted': deleted, 'kept': kept, 'truncated': False}


def _exists(storage, name):
    try:
        return storage.exists(name)
    except ClientError:
        return False


def files_missing_from_storage():
    """[(model, field, pk)] for every record naming an object that is not there."""
    out = []
    for model, field in file_fields():
        storage = _storage_of(field)
        rows = (model._default_manager.exclude(**{f'{field.name}__isnull': True})
                .exclude(**{field.name: ''}).values_list('pk', field.name))
        for pk, name in rows:
            if not _exists(storage, name):
                out.append((model, field, pk))
    return out


def clear_missing(missing):
    """Empty each field in `missing`; returns how many were cleared."""
    for model, field, pk in missing:
        model._default_manager.filter(pk=pk).update(**{field.name: None if field.null else ''})
    return len(missing)
