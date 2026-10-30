"""The permission catalogue and the eight seeded roles.

PERMISSIONS ARE STRINGS, AND THE LIST IS CLOSED
-----------------------------------------------
`ALL_PERMISSIONS` is the whole vocabulary. A role carrying a permission
that is not in it is a typo that would otherwise grant nothing silently —
the worst kind of access bug, because it fails open on the next refactor
when somebody "fixes" the spelling.

`pii.view` IS A PERMISSION, NOT A ROLE PROPERTY
-----------------------------------------------
The brief says the Analyst sees no PII. Making that a boolean on the role
would mean every future role has to remember to set it. As a permission it
is simply absent, and the masking layer asks one question of every caller:
"do you hold `pii.view` here?" Safety Officers get it so they can unmask a
contact during a live incident; Analysts never do.

SUPER ADMIN IS THE ONLY GLOBAL ROLE
------------------------------------
Everything else is held somewhere: a country, a state, a city. That is what
stops an India hire quietly acquiring the USA.
"""

# --- the catalogue --------------------------------------------------------

PERMISSIONS = {
    # Reading
    'trip.read': 'View trips and their fare breakdowns',
    'rider.read': 'View rider profiles and history',
    'driver.read': 'View driver profiles and history',
    'support.read': 'View support tickets',
    'finance.read': 'View ledgers, payouts and settlements',
    'report.read': 'View dashboards and reports',
    'audit.read': 'View the admin audit log',
    # Reading the pricing configuration -- service zones and rate cards --
    # is separate from changing it (`pricing.write`) and from drawing
    # geofences (`zone.write`). A City Ops Manager, Finance and an Analyst
    # need the card that produced a quote; Support and KYC do not.
    'zone.read': 'View service zones and rate cards',

    # Personal data. Held separately from every read above, so a role can
    # see the work without seeing the person.
    'pii.view': 'Reveal masked personal data (each reveal is logged)',

    # Acting
    'trip.write': 'Cancel, adjust or flag a trip',
    # Rider actions are separate capabilities (owner ruling, AUDIT 27.32).
    # `rider.write` no longer gates any rider action; it survives only for
    # creating QA test accounts (`auth_user.invite_views`), Super Admin only.
    'rider.write': 'Create rider test accounts (QA only)',
    'rider.note': 'Add internal notes to a rider',
    'rider.flag': 'Flag and unflag a rider for risk review',
    'rider.signout': 'Sign a rider out of every session',
    'rider.block': 'Block and unblock a rider (and sign them out)',
    'driver.write': 'Suspend, reinstate or amend a driver account',
    'driver.kyc': 'Approve or reject driver and vehicle documents',
    'driver.deactivate': 'Permanently deactivate a driver',
    'support.reply': 'Reply to and resolve support tickets',
    'support.refund': 'Issue a refund within the role limit',
    # Super Admin only by default: macros speak for the company to riders and
    # drivers, and the SLA policy sets what "late" means for every ticket.
    'support.macro': 'Create, edit and deactivate support macros',
    'support.sla': 'Change the support SLA policy',
    'support.dashboard': 'View the support SLA dashboard',
    'finance.payout': 'Approve and execute payout runs',
    'finance.adjust': 'Post a ledger adjustment',
    'pricing.write': 'Change fare cards, multipliers and commission',
    'zone.write': 'Draw and publish zones and geofences',
    'promo.write': 'Create and amend promotions and incentives',
    'notify.send': 'Send campaigns to riders or drivers',
    # Read-only door to the Safety queue (owner ruling, AUDIT 27.40): City Ops
    # may look at their own scope's incidents and signals; every write, the
    # assignee list and the SOS contact reveal stay `safety.respond`.
    'safety.read': 'View the Safety queue and incidents',
    'safety.respond': 'Act on SOS and incidents',
    # Super Admin only by default: the timers, thresholds and emergency
    # numbers that decide when an SOS escalates and what detection raises.
    'safety.config': 'Change the per-country safety timers, thresholds and numbers',
    'fraud.review': 'Review and action fraud flags',

    # Running the platform itself
    'admin.manage': 'Create admins, roles and assignments',
    'platform.settings': 'Cities, feature flags, app versions',
    'approval.check': 'Act as the second approver on a maker-checker request',
}

ALL_PERMISSIONS = frozenset(PERMISSIONS)

#: The role code that may only ever be held at Global.
SUPER_ADMIN = 'super_admin'

#: How many active Super Admins the platform must keep. A floor that
#: demotion is blocked from crossing -- NOT a requirement to fill
#: immediately, so a fresh environment is not forced to promote a test
#: account to reach it.
MIN_SUPER_ADMINS = 2


# --- the eight seeded roles ----------------------------------------------

ROLE_TEMPLATES = [
    {
        'code': SUPER_ADMIN,
        'name': 'Super Admin',
        'description': 'Everything, everywhere. Manages admins and roles. '
                       'Held only at Global, and only by named individuals.',
        'is_global_only': True,
        'permissions': sorted(ALL_PERMISSIONS),
    },
    {
        'code': 'city_ops_manager',
        'name': 'City Ops Manager',
        'description': 'Runs live operations for their scope: drivers, '
                       'zones, incentives. Proposes pricing; cannot publish it.',
        'permissions': [
            'trip.read', 'trip.write', 'rider.read', 'driver.read',
            'driver.write', 'support.read', 'report.read', 'zone.read',
            'zone.write', 'promo.write', 'pii.view',
            'rider.note', 'rider.flag', 'rider.signout', 'rider.block',
            # Read-only Safety queue within scope; no write, no reveal.
            'safety.read',
            # SLA numbers for their own country, through a checker.
            'support.sla', 'support.dashboard',
        ],
    },
    {
        'code': 'support_agent',
        'name': 'Support Agent',
        'description': 'Handles tickets and issues refunds up to the '
                       'configured limit. Sees masked contact details.',
        'permissions': [
            'trip.read', 'rider.read', 'driver.read',
            'support.read', 'support.reply', 'support.refund',
            'rider.note', 'rider.flag', 'rider.signout',
        ],
    },
    {
        'code': 'support_lead',
        'name': 'Support Lead',
        'description': 'Leads the support desk: tickets, refunds up to the '
                       'limit, saved replies and the SLA dashboard. Same '
                       'scope rules as every role; assigned by people.',
        'permissions': [
            'trip.read', 'rider.read', 'driver.read',
            'support.read', 'support.reply', 'support.refund',
            'support.macro', 'support.dashboard',
            'rider.note', 'rider.flag', 'rider.signout',
        ],
    },
    {
        'code': 'safety_officer',
        'name': 'Safety Officer',
        'description': 'SOS queue, incidents and emergency actions. May '
                       'unmask a contact during a live incident.',
        'permissions': [
            'trip.read', 'rider.read', 'driver.read', 'driver.write',
            'safety.respond', 'safety.read', 'support.read',
            'rider.note', 'rider.flag', 'rider.signout', 'rider.block',
            # The one role that must be able to reach a person immediately.
            'pii.view',
        ],
    },
    {
        'code': 'kyc_verifier',
        'name': 'KYC Verifier',
        'description': 'Reviews driver and vehicle documents. Sees identity '
                       'documents, which is the job, and nothing else: no '
                       'trips, and only drivers still in KYC.',
        # NO `trip.read` (owner, QA survey 2026-10-28: "KYC sees only KYC
        # queues"). Holding `driver.kyc` without `driver.write` or
        # `trip.read` is what `driver.admin_views.kyc_only` keys on to
        # narrow the driver list to drivers still in KYC.
        'permissions': [
            'driver.read', 'driver.kyc', 'pii.view',
        ],
    },
    {
        'code': 'finance',
        'name': 'Finance',
        'description': 'Payouts, settlements, ledgers, refunds above the '
                       'support limit, invoices and reconciliation.',
        'permissions': [
            'trip.read', 'driver.read', 'rider.read', 'support.read',
            'finance.read', 'finance.payout', 'finance.adjust',
            'support.refund', 'report.read', 'zone.read', 'pii.view',
            'rider.note',
        ],
    },
    {
        'code': 'marketing',
        'name': 'Marketing',
        'description': 'Promotions, coupons, referrals and rider '
                       'communications. No access to individuals.',
        'permissions': [
            'promo.write', 'notify.send', 'report.read',
        ],
    },
    {
        'code': 'analyst',
        'name': 'Read-only / Analyst',
        'description': 'Dashboards and reports. Explicitly NO personal data '
                       '— `pii.view` is absent and must stay absent.',
        'permissions': [
            'trip.read', 'driver.read', 'rider.read', 'report.read',
            # Reports need rate context; a rate card names no person.
            'zone.read',
        ],
    },
]


def validate_templates():
    """Every seeded permission must exist in the catalogue.

    Called by the seed command and by a test, so a typo in a role template
    fails loudly instead of granting nothing.
    """
    problems = []
    for template in ROLE_TEMPLATES:
        unknown = set(template['permissions']) - ALL_PERMISSIONS
        if unknown:
            problems.append(f"{template['code']}: unknown {sorted(unknown)}")
    return problems
