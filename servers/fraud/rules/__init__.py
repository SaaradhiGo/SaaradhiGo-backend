"""The detection rules, one module each. `RULES` is the closed list, in the order the console shows.

Every rule is a pure reader (see `rules.base`); `fraud.raising` turns hits into flags and
`fraud.tasks.run_rules` runs them. Adding a rule means a module here, a kind in `fraud.models`, a
unit test with a positive and a negative case, and nothing else: thresholds are per-country
`RiskRuleConfig` rows seeded from the module's `Param` defaults.
"""

from . import (  # noqa: F401
    cancellation_abuse, collusion, gps_anomaly, incentive_farming, multi_accounting,
    payment_abuse, referral_abuse,
)

RULES = {
    module.RULE.key: module.RULE
    for module in (referral_abuse, multi_accounting, collusion, incentive_farming, gps_anomaly,
                   cancellation_abuse, payment_abuse)
}

#: Rules the beat task evaluates (referral_abuse is raised by the reward check instead).
SCHEDULED = tuple(key for key, rule in RULES.items() if rule.params)


def rule_for(key):
    try:
        return RULES[key]
    except KeyError:
        raise ValueError(f'Unknown fraud rule {key!r}') from None
