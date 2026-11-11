"""Shared builders for the Fraud & Risk tests (module 13).

Low-entropy fixture values only (the secret scan reads every commit). No network. Built on the
Promotions world (Hyderabad and Austin, every role) so the scope rules are the real ones.
"""

from datetime import timedelta
from io import StringIO

from django.core.management import call_command
from django.utils import timezone

from conftest import seed_actor
from promotions_support import (  # noqa: F401
    block_the_network, body_of, client_for, completed_trip, error_code, geo, get, hyd_driver,
    make_rider, operator, post, world,
)
from servers.fraud.models import FlagEvent, RiskFlag

API = '/api/v1/fraud'
ADM = f'{API}/admin'


def seed_rules():
    call_command('seed_fraud_rules', actor_email=seed_actor().email, stdout=StringIO())


def make_flag(subject, node, *, kind='multi_accounting', severity='medium', score=50,
              status='open', evidence=None, age_hours=0, rule_version=1, key=None):
    """A flag as a rule would have raised it, anchored at `node`."""
    now = timezone.now()
    flag = RiskFlag.objects.create(
        subject=subject, kind=kind, rule_key=kind, rule_version=rule_version, severity=severity,
        score=score, status=status, anchor_node=node,
        dedupe_key=(key if key is not None else f'{kind}:{subject.pk}') if status in (
            'open', 'in_review') else '',
        evidence=evidence if evidence is not None else {
            'signals': ['same_device'], 'refs': {}, 'measures': {}, 'hit_count': 1,
            'first_seen': now.isoformat(), 'last_seen': now.isoformat()},
        last_seen_at=now)
    if age_hours:
        RiskFlag.objects.filter(pk=flag.pk).update(created_at=now - timedelta(
            hours=age_hours))
    FlagEvent.objects.create(flag=flag, action='raised', to_status=status)
    return flag


def reviewer(world_ns, geo_ns, node='IN-TG-HYD'):
    """Another City Ops Manager in the same city."""
    return operator('city_ops_manager', geo_ns, node)


def flag_url(flag, tail=''):
    return f'{ADM}/flags/{flag.pk}/{tail}'
