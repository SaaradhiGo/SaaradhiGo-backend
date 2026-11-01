"""The frozen console, route by route: what each screen shows and what replaces it.

WHY THIS IS A DATA FILE
-----------------------
The owner's decision (AUDIT.md section 10, item 1) is that Next.js is the admin
and the Django console is frozen: no new features, security fixes only, reachable
only by a Super Admin, and only on routes a deployment has allowlisted. Retired
screen by screen.

"Screen by screen" needs a list of the screens. This is that list, and it does
three jobs at once so that it cannot drift from any of them:

  1. It is the PARITY TABLE. Every route carries what it shows and which Next.js
     module takes it over, so retiring a screen is deleting a line here once the
     module ships.
  2. It is the BOOT GUARD's vocabulary. `FROZEN_CONSOLE_ALLOWED_ROUTES` is read
     from the environment, and production refuses to start if it names a route
     that is not in this file (`base/settings.py`). Settings are imported before
     Django's URL resolver exists, so the vocabulary has to be static -- hence a
     module with no Django imports.
  3. It is what the TEST SUITE checks the live resolver against
     (`tests/test_frozen_console_lockdown.py`). A route added to the console or a
     model registered with the admin site appears in the resolver and not here,
     and the suite fails until somebody writes its line -- what it shows, what
     replaces it. A new screen cannot slip onto the frozen surface unlisted.

Routes are identified by Django view name (`fleet_monitor`, `admin:index`,
`admin:driver_driver_changelist`) rather than by path, because the admin site's
paths carry converters and a regex and the names are what `reverse()` and the
resolver agree on.

THE NEXT.JS MODULES
-------------------
Named exactly as the module plan lists them, in its order, so a replacement here
can be matched to a module there by string: Command Centre, Trips, Drivers & KYC,
Riders, Support, Safety, Payments/Payouts/Finance, Pricing & Fares, Zones, Live
Ops Map, Notifications, Promotions & Incentives, Fraud & Risk, Reports, Platform
Settings & Integrations.
"""

from collections import namedtuple

Route = namedtuple('Route', ('name', 'shows', 'replaced_by'))

# ---------------------------------------------------------------------------
# The session console at the site root (servers/admin_dashboard/urls.py)
# ---------------------------------------------------------------------------

CONSOLE_ROUTES = (
    Route('login',
          'Operator sign-in: phone + password, brute-force guarded',
          'Next.js sign-in (Command Centre)'),
    Route('ops_mfa_challenge',
          'Second-factor challenge for a session whose password succeeded',
          'Next.js sign-in (Command Centre)'),
    Route('logout', 'Ends the console session', 'Next.js sign-in (Command Centre)'),
    Route('fleet_monitor',
          'Live fleet map with the day\'s headline KPIs',
          'Command Centre + Live Ops Map'),
    Route('driver_onboarding',
          'KYC queue: document checklist, approve / reject a driver',
          'Drivers & KYC'),
    Route('dispute_support',
          'Support tickets and disputes, replies and resolution',
          'Support'),
    Route('payment_dashboard',
          'Cashfree ledger, rider payments, driver payout transactions, webhook log',
          'Payments/Payouts/Finance'),
    Route('executive_revenue',
          'GBV, platform revenue, take rate, vehicle-class split, growth series',
          'Reports'),
    Route('driver_loyalty',
          'Driver loyalty tiers and incentive standings',
          'Promotions & Incentives'),
    Route('ride', 'Trip list with status filters', 'Trips'),
    Route('ride_detail', 'One trip: fare breakdown, timeline, parties', 'Trips'),
    Route('riders', 'Rider list with wallet balances', 'Riders'),
    Route('promo_codes', 'Promo code list, create and amend', 'Promotions & Incentives'),
    Route('emergency_dashboard', 'SOS queue and acknowledgements', 'Safety'),
    Route('stale_rides',
          'Active rides that stopped looking alive: mark reviewed, release driver',
          'Trips (Live Ops)'),
    Route('transaction_dashboard',
          'Transaction reconciliation across methods and markets',
          'Payments/Payouts/Finance'),
    Route('fare_surge', 'Surge multipliers and per-zone fare configuration', 'Pricing & Fares'),
    Route('predictive_heatmaps', 'Demand heatmaps by hour and area', 'Live Ops Map'),
    Route('global_search', 'Search drivers, trips and accounts', 'Command Centre'),
    Route('global_search_api', 'JSON behind the search box', 'Command Centre'),
    Route('driver_profile',
          'One driver: identity documents, vehicles, revenue, actions',
          'Drivers & KYC'),
    Route('notifications', 'Operator notification page', 'Notifications'),
    Route('admin_notifications_feed', 'JSON feed behind the console bell', 'Notifications'),
    Route('admin_notification_mark_read', 'Marks one bell item read', 'Notifications'),
)

# ---------------------------------------------------------------------------
# The Django admin site (django.contrib.admin, mounted at /admin/)
# ---------------------------------------------------------------------------

ADMIN_SITE_ROUTES = (
    Route('admin:index',
          'Index of registered models; the break-glass landing page',
          'Command Centre'),
    Route('admin:login',
          'Password-only sign-in with no throttle and no second factor',
          'Next.js sign-in (Command Centre); never allowlisted, see NEVER_ALLOWED'),
    Route('admin:logout', 'Ends the session', 'Next.js sign-in (Command Centre)'),
    Route('admin:password_change',
          'Changes the signed-in operator\'s password outside the audited path',
          'Platform Settings & Integrations; never allowlisted'),
    Route('admin:password_change_done', 'Confirmation of the above', 'Platform Settings & Integrations; never allowlisted'),
    Route('admin:autocomplete', 'JSON autocomplete for admin forms', 'none (admin plumbing)'),
    Route('admin:jsi18n', 'JavaScript translation catalogue', 'none (admin plumbing)'),
    Route('admin:view_on_site', 'Redirect from a content type + id to its page', 'none (admin plumbing)'),
    Route('admin:app_list', 'Per-app model index', 'Command Centre'),
)

#: (app_label, model_name, replacement module) for every model registered
#: with the admin site -- by this repository's admin.py files and by the
#: third-party apps that register their own (auth, simplejwt's blacklist,
#: django-otp). Each contributes the five standard ModelAdmin routes below.
ADMIN_SITE_MODELS = (
    ('auth', 'group', 'Platform Settings & Integrations (roles live in admin_access)'),
    ('token_blacklist', 'outstandingtoken', 'Platform Settings & Integrations (sessions)'),
    ('token_blacklist', 'blacklistedtoken', 'Platform Settings & Integrations (sessions)'),
    ('auth_user', 'customuser', 'Riders + Drivers & KYC + Platform Settings & Integrations (operators)'),
    ('rider', 'rider', 'Riders'),
    ('rider', 'favoriteplace', 'Riders'),
    ('rider', 'wallet', 'Payments/Payouts/Finance'),
    ('rider', 'notification', 'Notifications'),
    ('rider', 'wallettransaction', 'Payments/Payouts/Finance'),
    ('driver', 'driver', 'Drivers & KYC'),
    ('driver', 'vehicle', 'Drivers & KYC'),
    ('driver', 'vehicletype', 'Pricing & Fares'),
    ('driver', 'withdrawalrequest', 'Payments/Payouts/Finance'),
    ('driver', 'driverupicontact', 'Payments/Payouts/Finance'),
    ('driver', 'driverbankaccount', 'Payments/Payouts/Finance'),
    ('ride', 'tripstatus', 'Trips'),
    ('ride', 'trip', 'Trips'),
    ('ride', 'farepricing', 'Pricing & Fares'),
    ('ride', 'vehiclefarepricing', 'Pricing & Fares'),
    ('ride', 'rating', 'Trips'),
    ('ride', 'receipt', 'Trips'),
    ('ride', 'chatmessage', 'Support'),
    ('ride', 'promocode', 'Promotions & Incentives'),
    ('ride', 'promoredemption', 'Promotions & Incentives'),
    ('payments', 'payment', 'Payments/Payouts/Finance'),
    ('payments', 'transactionhistory', 'Payments/Payouts/Finance'),
    ('pricing', 'servicezone', 'Zones'),
    ('pricing', 'ratecard', 'Pricing & Fares'),
    ('support', 'supportticket', 'Support'),
    ('support', 'supportmessage', 'Support'),
    ('otp_totp', 'totpdevice', 'Platform Settings & Integrations (operator MFA)'),
    ('otp_static', 'staticdevice', 'Platform Settings & Integrations (operator MFA)'),
)

#: The five views every ModelAdmin mounts, in the order Django mounts them.
MODEL_VIEW_SUFFIXES = ('changelist', 'add', 'history', 'delete', 'change')

#: Routes a ModelAdmin adds beyond the five. django-otp's TOTP admin serves
#: the device's provisioning URI and its QR code -- the shared secret itself.
ADMIN_SITE_EXTRA_MODEL_ROUTES = (
    Route('admin:otp_totp_totpdevice_config',
          'The provisioning URI (shared secret) of any operator\'s authenticator',
          'nothing; the secret is shown once at enrolment and never again'),
    Route('admin:otp_totp_totpdevice_qrcode',
          'The same secret as a QR code',
          'nothing; never allowlisted'),
)


def model_routes():
    """The five standard routes of every registered model, as Route rows."""
    rows = []
    for app_label, model_name, replaced_by in ADMIN_SITE_MODELS:
        for suffix in MODEL_VIEW_SUFFIXES:
            rows.append(Route(
                f'admin:{app_label}_{model_name}_{suffix}',
                f'{app_label}.{model_name} {suffix}',
                replaced_by,
            ))
    return tuple(rows)


def all_routes():
    """Every route on the frozen surface, console first, in parity-table order."""
    return CONSOLE_ROUTES + ADMIN_SITE_ROUTES + model_routes() + ADMIN_SITE_EXTRA_MODEL_ROUTES


def known_routes():
    """The vocabulary `FROZEN_CONSOLE_ALLOWED_ROUTES` may draw from."""
    return frozenset(route.name for route in all_routes())


# ---------------------------------------------------------------------------
# Policy constants
# ---------------------------------------------------------------------------

#: Reachable without being a Super Admin, because they are how one becomes a
#: signed-in, second-factor-verified Super Admin in the first place -- or
#: stops being one. Not configurable: an allowlist that could drop the
#: sign-in would lock the break-glass account out, and one that could drop
#: the sign-out would leave a privileged session nobody can end. Each view
#: carries its own guard (`is_operator`, the login throttle, the MFA check).
ENTRY_ROUTES = frozenset({
    'login',
    'ops_mfa_challenge',
    'logout',
    'admin:logout',
})

#: Refused even when configured, and a production boot fault if they are.
#:
#: `admin:login` is a password-only door with no throttle and no second
#: factor beside a console sign-in that has both; allowlisting it would make
#: the stronger door decorative. The password-change pair bypasses the
#: audited credential path that revokes the account's other sessions. The two
#: TOTP routes display another operator's shared secret, which is the one
#: thing an MFA deployment must never show anybody after enrolment.
NEVER_ALLOWED = frozenset({
    'admin:login',
    'admin:password_change',
    'admin:password_change_done',
    'admin:otp_totp_totpdevice_config',
    'admin:otp_totp_totpdevice_qrcode',
})

#: What a deployment gets when it sets nothing. Deliberately one entry: the
#: admin index shows which models are registered and nothing else, which
#: makes it a safe place to prove that a break-glass session works (sign-in,
#: second factor, audit row) before the owner allowlists the one screen an
#: incident actually needs. Nothing on this list shows a rider, a driver, a
#: rupee or a secret.
DEFAULT_ALLOWED_ROUTES = ('admin:index',)


def parse_routes(value):
    """`FROZEN_CONSOLE_ALLOWED_ROUTES` as a tuple of names, however it was given.

    The environment gives a comma-separated string; a test or a settings
    module may give a list. Blank entries are dropped so a trailing comma is
    not a route named ''.
    """
    if value is None:
        return ()
    if isinstance(value, str):
        parts = value.split(',')
    else:
        parts = list(value)
    return tuple(dict.fromkeys(str(p).strip() for p in parts if str(p).strip()))


def configuration_faults(configured):
    """Why this allowlist must not ship, as a list of sentences; empty if it may.

    Two kinds of fault. A name nobody recognises is a typo, and a typo in an
    allowlist fails silently -- the route is simply never reachable, which
    somebody discovers during an incident. A name on `NEVER_ALLOWED` is a
    decision being reversed through an environment variable, which is not
    where that decision gets reversed.
    """
    configured = parse_routes(configured)
    known = known_routes()
    faults = []
    unknown = sorted(name for name in configured if name not in known)
    if unknown:
        faults.append(
            f'FROZEN_CONSOLE_ALLOWED_ROUTES names routes that are not in the '
            f'frozen console inventory: {unknown}. Each entry must be a view '
            f'name listed in servers/admin_dashboard/inventory.py.'
        )
    forbidden = sorted(name for name in configured if name in NEVER_ALLOWED)
    if forbidden:
        faults.append(
            f'FROZEN_CONSOLE_ALLOWED_ROUTES names routes the frozen console '
            f'refuses to serve under any configuration: {forbidden}. See '
            f'NEVER_ALLOWED in servers/admin_dashboard/inventory.py for why.'
        )
    return faults
