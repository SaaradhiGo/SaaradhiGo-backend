"""The actions maker-checker knows about, and what each one costs to check.

WHY A CLOSED LIST
-----------------
A policy row names an action by a string. If any string were accepted, a
typo in a caller (`finance.payouts`) would look up no policy, and the
caller would be told the country has no threshold configured -- the US
refusal, fired in India, for a spelling mistake. The list is closed for the
same reason `admin_access.roles.ALL_PERMISSIONS` is: an unknown key is a
defect at the boundary, not data.

WHAT A CHECKER MUST HOLD
------------------------
`approval.check` says "may act as the second pair of eyes". It does not say
on what. A Marketing admin holding it must not be the one who releases a
payout run, so every action also names the capability its OWN maker needs,
and the checker must hold that too, in the request's scope. Two finance
admins, not one finance admin and anybody.

`always_checker` marks the one action whose rule is built in rather than
read from a policy row: changing a policy. If that needed a policy row, a
country with no rows could never be given one through the framework.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Action:
    key: str
    description: str
    #: The capability a maker needs to raise this action, which the checker
    #: must ALSO hold in the request's scope.
    capability: str
    #: True for the built-in rule: no policy row is consulted, a checker is
    #: always required, in every country.
    always_checker: bool = False
    #: A payload key naming MANY targets (a payout run: withdrawal_ids). A request
    #: carrying it always needs a checker, whatever the policy's maker_limit says:
    #: the limit is for ONE target. Generic, so every country behaves the same.
    multi_target_key: str = ''
    #: A payload key that, when truthy, makes THIS request always need a checker whatever the
    #: policy's maker limit says (a fraud recovery under `finance.adjust`: owner ruling, AUDIT 27.56).
    force_checker_key: str = ''


SUPPORT_REFUND = 'support.refund'
FINANCE_REFUND = 'finance.refund'
FINANCE_ADJUST = 'finance.adjust'
FINANCE_PAYOUT = 'finance.payout'
PRICING_CHANGE = 'pricing.change'
DRIVER_DEACTIVATE = 'driver.deactivate'
BULK_ACTION = 'bulk.action'
POLICY_CHANGE = 'approvals.policy_change'
THRESHOLD_CHANGE = 'command_centre.threshold_change'
SUPPORT_SLA_CHANGE = 'support.sla_change'
FINANCE_PAYOUT_CONFIG = 'finance.payout_config_change'
FINANCE_WALLET_CREDIT = 'finance.wallet_credit'
ZONE_CHANGE = 'zone.change'
NOTIFICATION_TEMPLATE_CHANGE = 'notification.template_change'
NOTIFICATION_CAMPAIGN_SEND = 'notification.campaign_send'
PROMOTION_LAUNCH = 'promotion.launch'
PROMOTION_BUDGET_INCREASE = 'promotion.budget_increase'
FRAUD_RULE_CHANGE = 'fraud.rule_change'
FINANCE_PAYOUT_HOLD = 'finance.payout_hold'

ACTIONS = {
    action.key: action for action in (
        Action(SUPPORT_REFUND, 'Refund issued by support on one trip', 'support.refund'),
        Action(FINANCE_REFUND, 'Refund issued by finance', 'support.refund'),
        Action(FINANCE_ADJUST, 'Manual ledger adjustment', 'finance.adjust',
               force_checker_key='requires_checker'),
        Action(FINANCE_PAYOUT, 'Payout run: release withdrawals to drivers', 'finance.payout',
               multi_target_key='withdrawal_ids'),
        Action(PRICING_CHANGE, 'Change to fare cards, multipliers or commission', 'pricing.write'),
        Action(DRIVER_DEACTIVATE, 'Permanent deactivation of a driver', 'driver.deactivate'),
        Action(BULK_ACTION, 'Bulk action over many records', 'admin.manage'),
        Action(POLICY_CHANGE, 'Change to a maker-checker threshold', 'platform.settings',
               always_checker=True),
        # Command Centre alert lines are config, and config is a maker-checker
        # action (owner, AUDIT.md section 10). Anchored at the city.
        Action(THRESHOLD_CHANGE, 'Change to a Command Centre alert threshold', 'platform.settings',
               always_checker=True),
        # A support SLA value (or a country's waiting / refund-review numbers).
        # Always a checker: the policy row is `always`, seeded per country.
        Action(SUPPORT_SLA_CHANGE, 'Change to a support SLA or country support setting',
               'support.sla'),
        # Payout rules (fee, minimum, stuck hours, schedule) for a country. A
        # policy row per country decides it; the seeded rule is `always`.
        Action(FINANCE_PAYOUT_CONFIG, 'Change to a country payout setting', 'finance.config'),
        # A closed-loop credit added to a rider wallet by an operator.
        Action(FINANCE_WALLET_CREDIT, 'Credit added to a rider wallet', 'finance.adjust'),
        # A service zone or geofence: create, change polygon or settings, deactivate,
        # reactivate or restore a revision. Always a checker (module 9).
        Action(ZONE_CHANGE, 'Change to a service zone or geofence', 'zone.write'),
        # Module 11: a message template going live (or retired), and a promotional broadcast
        # being sent. Both always need a checker, in India and the US.
        Action(NOTIFICATION_TEMPLATE_CHANGE, 'Activate or retire a notification template',
               'notify.template'),
        Action(NOTIFICATION_CAMPAIGN_SEND, 'Send a promotional campaign', 'notify.campaign'),
        # Module 12: a promotion, driver incentive or referral campaign going live (any kind), and
        # a campaign budget being raised. Both always need a checker, in India and the US. The
        # maker's own capability (promo.write for rider promos and rider referrals, incentive.write
        # for driver incentives and driver referrals) is checked by the module, at the campaign's
        # place, before the request is raised; the checker holds approval.check and can see the
        # campaign and its budget (promo.read) in that scope.
        Action(PROMOTION_LAUNCH, 'Launch a promotion, incentive or referral campaign',
               'promo.read'),
        Action(PROMOTION_BUDGET_INCREASE, 'Increase a campaign budget', 'promo.read'),
        # Module 13: a detection rule's thresholds (or switching it off). Always a checker, in India
        # and the US. The checker must hold fraud.rules too, so it is a second Super Admin.
        Action(FRAUD_RULE_CHANGE, 'Change a fraud detection rule threshold', 'fraud.rules'),
        # Fraud money actions (AUDIT 27.56): a full payout hold on a suspended driver, for serious
        # fraud. Always a checker, India and US; the checker must be a second finance.payout holder.
        Action(FINANCE_PAYOUT_HOLD, 'Hold a suspended driver\'s payouts (serious fraud)',
               'finance.payout', always_checker=True),
    )
}

ALL_ACTION_KEYS = frozenset(ACTIONS)


def action_for(key):
    """The catalogue entry, or raise: an unknown key is a caller defect."""
    try:
        return ACTIONS[key]
    except KeyError:
        raise ValueError(f'Unknown maker-checker action {key!r}') from None
