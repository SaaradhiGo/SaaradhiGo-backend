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


SUPPORT_REFUND = 'support.refund'
FINANCE_REFUND = 'finance.refund'
FINANCE_ADJUST = 'finance.adjust'
FINANCE_PAYOUT = 'finance.payout'
PRICING_CHANGE = 'pricing.change'
DRIVER_DEACTIVATE = 'driver.deactivate'
BULK_ACTION = 'bulk.action'
POLICY_CHANGE = 'approvals.policy_change'

ACTIONS = {
    action.key: action for action in (
        Action(SUPPORT_REFUND, 'Refund issued by support on one trip', 'support.refund'),
        Action(FINANCE_REFUND, 'Refund issued by finance', 'support.refund'),
        Action(FINANCE_ADJUST, 'Manual ledger adjustment', 'finance.adjust'),
        Action(FINANCE_PAYOUT, 'Payout run: release withdrawals to drivers', 'finance.payout'),
        Action(PRICING_CHANGE, 'Change to fare cards, multipliers or commission', 'pricing.write'),
        Action(DRIVER_DEACTIVATE, 'Permanent deactivation of a driver', 'driver.deactivate'),
        Action(BULK_ACTION, 'Bulk action over many records', 'admin.manage'),
        Action(POLICY_CHANGE, 'Change to a maker-checker threshold', 'platform.settings',
               always_checker=True),
    )
}

ALL_ACTION_KEYS = frozenset(ACTIONS)


def action_for(key):
    """The catalogue entry, or raise: an unknown key is a caller defect."""
    try:
        return ACTIONS[key]
    except KeyError:
        raise ValueError(f'Unknown maker-checker action {key!r}') from None
