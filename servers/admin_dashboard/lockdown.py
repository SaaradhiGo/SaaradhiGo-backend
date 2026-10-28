"""One gate in front of the frozen Django console, and a read-only admin site.

THE DECISION THIS ENFORCES
--------------------------
AUDIT.md section 10, item 1: Next.js is the admin. The Django console
(`servers/admin_dashboard`, mounted at the site root) and the Django admin
site (`/admin/`) are frozen -- security fixes only, restricted to a Super
Admin plus a configured allowlist of routes, retired screen by screen.

The alternative was to retrofit city scoping and PII masking into six
thousand lines of server-rendered views that are being deleted. A gate is
smaller, provably covers every route (the test suite walks the resolver),
and does not turn a retirement into a second maintenance burden.

WHAT "RESTRICTED" MEANS HERE, IN ORDER
--------------------------------------
For a request whose resolved route is on the frozen surface:

  1. The sign-in, the second-factor challenge and the sign-outs pass through
     untouched (`inventory.ENTRY_ROUTES`); each has its own guard and they
     are how a Super Admin gets a verified session at all.
  2. Anyone who is not an operator holding Super Admin at Global gets 404.
  3. Any route not on `FROZEN_CONSOLE_ALLOWED_ROUTES` gets 404, Super Admin
     or not.
  4. A Super Admin on an allowed route whose SESSION has not presented its
     second factor is sent to the challenge, exactly as the console has
     always done -- that redirect tells somebody who already proved a Super
     Admin's password nothing they do not know.
  5. The first visit to each allowed route in a session writes one audit row.

404 AND NOT 403
---------------
A 403 says "this exists and you may not have it". The frozen console is being
retired and must not advertise itself: to a rider, a scoped operator or an
anonymous scan, a retired screen and a URL that was never mounted look the
same. `Http404` is raised rather than a response built, so the refusal takes
exactly the path an unresolved URL takes and renders through the same
handler.

WHY A MIDDLEWARE AND NOT A DECORATOR
------------------------------------
The admin site's views are Django's, not ours, and a decorator has to be
remembered on every new view. `process_view` sees every request after the
resolver has named its route and decides by that name, so a view added to
the console tomorrow is gated the moment it is routed, and the inventory
test fails until its parity line is written.

THE ADMIN SITE IS READ-ONLY
---------------------------
Separately from who may reach it, every ModelAdmin refuses add, change and
delete unless its model is on `FROZEN_CONSOLE_WRITABLE_MODELS` (default:
none). Money, access and driver status change only through the audited API
paths; an admin form that could set `Driver.approved` or
`WithdrawalRequest.status` directly is a second, unaudited path, and the
registry here includes exactly those models. The site subclass wraps every
class at `register()`, so third-party registrations (auth, the token
blacklist, django-otp) are covered without touching their code.
"""

import logging

from django.conf import settings
from django.contrib import admin
from django.contrib.admin.apps import AdminConfig
from django.http import Http404
from django.shortcuts import redirect
from django.urls import NoReverseMatch, reverse

from . import inventory

logger = logging.getLogger(__name__)

#: Audit action written once per session per allowed route.
AUDIT_ACTION = 'frozen_console_access'
AUDIT_TARGET_TYPE = 'frozen_console_route'

#: Session key holding the route names already audited in this session.
SESSION_KEY = 'frozen_console_audited'

#: View modules that make a route part of the session console. Matched by
#: prefix so `views`, `notification_api` and any module added later count.
_CONSOLE_MODULE_PREFIX = 'servers.admin_dashboard.'

_CONSOLE_ROUTE_NAMES = frozenset(route.name for route in inventory.CONSOLE_ROUTES)


# ---------------------------------------------------------------------------
# Configuration, read per request so a test's `settings` override is honoured
# ---------------------------------------------------------------------------

def allowed_routes():
    """The route names a Super Admin may reach in this deployment.

    `NEVER_ALLOWED` is subtracted even though production refuses to boot with
    one configured: QA and development boot regardless, and the refusal must
    hold there too or QA rehearses a console production will not serve.
    """
    raw = getattr(settings, 'FROZEN_CONSOLE_ALLOWED_ROUTES',
                  inventory.DEFAULT_ALLOWED_ROUTES)
    return frozenset(inventory.parse_routes(raw)) - inventory.NEVER_ALLOWED


def writable_models():
    """`app_label.modelname` labels whose ModelAdmin may still write. Default none."""
    raw = getattr(settings, 'FROZEN_CONSOLE_WRITABLE_MODELS', ())
    return frozenset(label.lower() for label in inventory.parse_routes(raw))


def is_frozen_route(match):
    """Is this resolved route part of the frozen surface?

    Two signals, either sufficient: the view lives in `servers.admin_dashboard`
    (the session console, whatever its path -- it is mounted at the root, so a
    path prefix cannot identify it), or the route sits in the `admin`
    namespace (the admin site, including its unnamed redirect and catch-all
    routes). The console's known names are a third signal for the case where
    a view is wrapped by something that loses `__module__`.
    """
    if match is None:
        return False
    if 'admin' in (match.namespaces or ()):
        return True
    module = getattr(match.func, '__module__', '') or ''
    if module.startswith(_CONSOLE_MODULE_PREFIX):
        return True
    return match.view_name in _CONSOLE_ROUTE_NAMES


def holds_super_admin(user):
    """Operator AND Super Admin at Global, by the access model's own definition.

    `is_super_admin` is the one function the rest of the platform uses for
    this question. It counts a Django superuser as well as a `super_admin`
    assignment at the Global node -- the superuser is the documented
    break-glass account, and break-glass is exactly the case this console is
    kept alive for. `is_operator` is asked first so a non-operator account
    that somehow carried a flag is still refused.
    """
    from base.permissions import is_operator
    from servers.admin_access.access import is_super_admin

    return bool(user) and is_operator(user) and is_super_admin(user)


def landing_url():
    """Where the sign-in sends a verified Super Admin.

    The console used to land on the fleet monitor. Under the lockdown that is
    a 404 unless the deployment allowlisted it, and a sign-in that lands on a
    404 reads as broken rather than restricted. So: the fleet monitor if it is
    allowed, else the admin index, else the first allowed route that needs no
    arguments. With nothing allowed it still returns the fleet monitor -- a
    dead end -- and never the sign-in, which redirects operators here and
    would loop.
    """
    allowed = allowed_routes()
    preferred = ('fleet_monitor', 'admin:index')
    candidates = list(preferred) + [
        route.name for route in inventory.all_routes()
        if route.name not in preferred
    ]
    for name in candidates:
        if name not in allowed:
            continue
        try:
            return reverse(name)
        except NoReverseMatch:
            continue
    return reverse('fleet_monitor')


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------

class FrozenConsoleMiddleware:
    """Applies the five rules in the module docstring to every frozen route.

    Must sit after `AuthenticationMiddleware` and `OTPMiddleware`: it reads
    `request.user`, and the second-factor check reads the `is_verified()`
    that OTPMiddleware attaches. It touches `request.user` only for frozen
    routes, so API requests pay nothing here.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        match = getattr(request, 'resolver_match', None)
        if not is_frozen_route(match):
            return None

        name = match.view_name or ''
        if name in inventory.ENTRY_ROUTES:
            return None

        user = getattr(request, 'user', None)
        if not holds_super_admin(user):
            raise Http404

        if not name or name not in allowed_routes():
            # Unnamed routes (the admin's per-object redirect and catch-all)
            # can never be allowlisted, so they are refused here as well.
            raise Http404

        from base import ops_mfa

        if not ops_mfa.console_access_allowed(request):
            return redirect('ops_mfa_challenge')

        _audit_once(request, name)
        return None


def _audit_once(request, name):
    """One `frozen_console_access` row per session per route.

    Per request would make the log unreadable for a page that polls; per
    session answers the question an access review asks, which is "who opened
    which retired screen, and when". The row is written before the session is
    marked, so an audit pipeline failure (which `record_admin_action` logs
    and swallows) is retried on the next request rather than lost.
    """
    from servers.admin_audit.services import record_admin_action

    session = getattr(request, 'session', None)
    seen = list(session.get(SESSION_KEY) or []) if session is not None else []
    if name in seen:
        return
    row = record_admin_action(
        request,
        action=AUDIT_ACTION,
        target_type=AUDIT_TARGET_TYPE,
        target_id=name,
        after={'path': request.path, 'method': request.method},
    )
    if row is not None and session is not None:
        session[SESSION_KEY] = seen + [name]


# ---------------------------------------------------------------------------
# The read-only admin site
# ---------------------------------------------------------------------------

class ReadOnlyModelAdminMixin:
    """Refuses add, change and delete unless the model is explicitly writable.

    Viewing is left to Django's own permission check. The result is an admin
    that can show a row to a Super Admin during an incident and cannot be
    used to change it: the changelist renders, the change form opens
    read-only, a POST to it is refused, bulk delete is not offered.
    """

    def _frozen_writable(self):
        return self.model._meta.label_lower in writable_models()

    def has_add_permission(self, request):
        return self._frozen_writable() and super().has_add_permission(request)

    def has_change_permission(self, request, obj=None):
        return self._frozen_writable() and super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        return self._frozen_writable() and super().has_delete_permission(request, obj)


class FrozenAdminSite(admin.AdminSite):
    """`admin.site` for this project: every registration arrives read-only.

    Wrapping at `register()` rather than editing each `admin.py` means the
    rule holds for classes this repository does not own -- `auth.Group`, the
    simplejwt blacklist, django-otp's devices -- and for any ModelAdmin added
    later without the author having to know about it.
    """

    def register(self, model_or_iterable, admin_class=None, **options):
        admin_class = admin_class or admin.ModelAdmin
        if not issubclass(admin_class, ReadOnlyModelAdminMixin):
            admin_class = type(
                f'Frozen{admin_class.__name__}',
                (ReadOnlyModelAdminMixin, admin_class),
                {'__module__': admin_class.__module__},
            )
        return super().register(model_or_iterable, admin_class, **options)


class FrozenAdminConfig(AdminConfig):
    """Stands in for `django.contrib.admin` in INSTALLED_APPS.

    `default_site` is how Django is told which class `admin.site` is, and it
    has to be decided before any `admin.py` runs -- which is why this is an
    AppConfig and not a call somewhere after autodiscovery.
    """

    default_site = 'servers.admin_dashboard.lockdown.FrozenAdminSite'
