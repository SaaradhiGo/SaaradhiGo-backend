"""Running the scheduled rules once. Called by the beat task and by tests."""

import logging

from django.utils import timezone

from . import config as rule_config
from .models import RiskRuleConfig
from .raising import CREATED, check_queue_aging, raise_hit
from .rules import RULES, SCHEDULED

logger = logging.getLogger(__name__)


def run_rule(rule, config, now=None):
    """Evaluate one rule for one country config. Returns `{outcome: count}`."""
    now = now or timezone.now()
    thresholds = rule_config.thresholds_of(rule, config)
    tally = {}
    for hit in rule.evaluate(thresholds, config.country, now):
        _flag, outcome = raise_hit(rule, config, hit, now)
        tally[outcome] = tally.get(outcome, 0) + 1
    RiskRuleConfig.objects.filter(pk=config.pk).update(last_run_at=now)
    return tally


def run_all(now=None):
    """Every enabled, configured scheduled rule in every country. One rule failing does not stop
    the others; the failure is logged and reported."""
    now = now or timezone.now()
    summary, failed = {}, []
    for config in RiskRuleConfig.objects.filter(is_active=True, enabled=True,
                                                rule_key__in=SCHEDULED).order_by('country', 'id'):
        key = f'{config.country}:{config.rule_key}'
        try:
            summary[key] = run_rule(RULES[config.rule_key], config, now)
        except Exception:  # noqa: BLE001 - one rule must not stop the rest
            logger.exception('fraud rule %s failed', key)
            failed.append(key)
    try:
        aging = check_queue_aging(now)
    except Exception:  # noqa: BLE001
        logger.exception('fraud queue aging check failed')
        aging = None
    created = sum(t.get(CREATED, 0) for t in summary.values())
    return {'rules': summary, 'created': created, 'failed': failed, 'aging_alerts': aging}
