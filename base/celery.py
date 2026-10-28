import os
from celery import Celery
from celery.signals import worker_init
from django.conf import settings
os.environ.setdefault('DJANGO_SETTINGS_MODULE','base.settings')
app=Celery('base')

app.config_from_object(settings,namespace='CELERY')
app.autodiscover_tasks()

# Do not consume tasks until the database is migrated. The backend runs
# `migrate` as its pre-deploy step and the worker does not, so without this a
# worker built from new code can start against the previous schema. See
# base/migration_gate.py for the signal choice and the off switch.
from base.migration_gate import hold_worker_until_migrated  # noqa: E402

worker_init.connect(hold_worker_until_migrated, dispatch_uid='base.migration_gate.worker_init')
