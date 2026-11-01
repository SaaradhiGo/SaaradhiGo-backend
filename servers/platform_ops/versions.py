"""App version policy: what the rider and driver apps must, and may, update to.

Per (app, platform): `minimum_version` (below it the user MUST update), `latest_version` (below it
they MAY), a store URL and a message. Exposed in the public app-config endpoint.

CHANGING IT
-----------
Capability `platform.settings` at Global. RAISING a minimum forces every user of an older build
off it, so it is a `platform.app_version` request (a checker). Lowering a minimum, moving the
latest version, the store URL and the message take effect immediately and are audited.

THE SERVER-SIDE GATE (what we chose)
------------------------------------
`check_request(request, app)` answers a stable `426 APP_UPDATE_REQUIRED` ONLY when the request
carries an `X-App-Version` header (with `X-App-Platform: android|ios`, and optionally
`X-App-Name`, defaulting to the endpoint's own app) that is BELOW the minimum. An ABSENT header,
an unparseable version or an unknown platform is allowed, so every app build in the field today
keeps working until a build that sends the header exists (App catch-up R9 / D9). It is wired into
the booking quote, the WebSocket booking and sign-in; nothing else.
"""

import re

from django.db import transaction

from servers.admin_access.bridge import granted
from servers.admin_access.models import GLOBAL, GeoNode
from servers.admin_audit.services import record_admin_action
from servers.approvals import service as approvals
from servers.approvals.actions import PLATFORM_APP_VERSION

from .errors import PlatformError
from .models import APP_CHOICES, PLATFORM_CHOICES, AppVersionPolicy

APPS = [a for a, _ in APP_CHOICES]
PLATFORMS = [p for p, _ in PLATFORM_CHOICES]
VERSION_RE = re.compile(r'^\d{1,4}(\.\d{1,4}){0,3}$')
AUDIT = 'app_version_policy_changed'
TARGET = 'app_version_policy'
UPDATE_REQUIRED = 'APP_UPDATE_REQUIRED'


def parse_version(text):
    """`(1, 4, 2)` from "1.4.2", "1.4.2+17" or "1.4.2-beta"; None if it is not a version."""
    base = re.split(r'[+\-\s]', str(text or '').strip(), maxsplit=1)[0]
    if not VERSION_RE.match(base):
        return None
    parts = tuple(int(p) for p in base.split('.'))
    return parts + (0,) * (4 - len(parts))


def _label(user):
    return (getattr(user, 'full_name', '') or getattr(user, 'email', '') or f'staff #{user.pk}')[:255]


def policy_dict(row):
    return {
        'app': row.app, 'platform': row.platform, 'minimum_version': row.minimum_version,
        'latest_version': row.latest_version, 'store_url': row.store_url,
        'message': row.message, 'version': row.version,
        'updated_by': row.updated_by_label or None,
        'updated_at': row.updated_at.isoformat() if row.updated_at else None,
    }


def table():
    """All four (app, platform) slots, with a row object or a blank placeholder."""
    rows = {(r.app, r.platform): r for r in AppVersionPolicy.objects.all()}
    out = []
    for app in APPS:
        for platform in PLATFORMS:
            row = rows.get((app, platform))
            out.append(policy_dict(row) if row else {
                'app': app, 'platform': platform, 'minimum_version': '0.0.0',
                'latest_version': '0.0.0', 'store_url': '', 'message': '', 'version': 0,
                'updated_by': None, 'updated_at': None})
    return out


def public_policy():
    """For app-config: `{app: {platform: {minimum, latest, store_url, message}}}`."""
    out = {app: {} for app in APPS}
    for r in AppVersionPolicy.objects.all():
        out[r.app][r.platform] = {
            'minimum_version': r.minimum_version, 'latest_version': r.latest_version,
            'store_url': r.store_url, 'message': r.message}
    return out


def _validate(app, platform, minimum, latest, store_url, message):
    if app not in APPS:
        raise PlatformError('app must be rider or driver.', code='INVALID_APP', field='app')
    if platform not in PLATFORMS:
        raise PlatformError('platform must be android or ios.', code='INVALID_PLATFORM',
                            field='platform')
    for field, value in (('minimum_version', minimum), ('latest_version', latest)):
        if parse_version(value) is None or not VERSION_RE.match(str(value)):
            raise PlatformError(f'{field} must look like 1.4.2.', code='INVALID_VERSION',
                                field=field)
    if parse_version(latest) < parse_version(minimum):
        raise PlatformError('The latest version cannot be below the minimum.',
                            code='LATEST_BELOW_MINIMUM', field='latest_version')
    store_url = str(store_url or '').strip()
    if store_url and not store_url.lower().startswith('https://'):
        raise PlatformError('The store URL must be an https:// link.', code='INVALID_URL',
                            field='store_url')
    if len(store_url) > 300 or len(str(message or '')) > 300:
        raise PlatformError('The store URL and the message are at most 300 characters.',
                            code='TOO_LONG', field='message')
    return store_url


def apply_policy(app, platform, minimum, latest, store_url, message, *, actor, reason,
                 request=None, approval=None):
    with transaction.atomic():
        row = (AppVersionPolicy.objects.select_for_update()
               .filter(app=app, platform=platform).first())
        before = policy_dict(row) if row else {}
        if row is None:
            row = AppVersionPolicy(app=app, platform=platform, version=0)
        new = (minimum, latest, store_url, message or '')
        if row.pk and (row.minimum_version, row.latest_version, row.store_url,
                       row.message) == new:
            return row, False
        row.minimum_version, row.latest_version, row.store_url, row.message = new
        row.version += 1
        row.updated_by_id = getattr(actor, 'pk', None)
        row.updated_by_label = _label(actor) if actor is not None else ''
        row.save()
        record_admin_action(
            request, action=AUDIT, target_type=TARGET, target_id=f'{app}:{platform}',
            before=before, after={**policy_dict(row),
                                  'approval_request_id': getattr(approval, 'pk', None)},
            reason=reason)
    return row, True


def propose_change(actor, *, app, platform, minimum_version=None, latest_version=None,
                   store_url=None, message=None, reason='', http_request=None):
    """Change a slot now, or raise a `platform.app_version` request when the minimum goes UP.
    Returns `(row_or_None, outcome_or_None)`."""
    reason = (reason or '').strip()
    if len(reason) < 5:
        raise PlatformError('A reason of at least 5 characters is required.',
                            code='REASON_REQUIRED', field='reason')
    root = GeoNode.objects.filter(kind=GLOBAL).first()
    if not granted(actor, 'platform.settings', root)[0]:
        raise PlatformError('Changing an app version needs platform.settings.',
                            code='SCOPE_NOT_HELD', field='app', status=403)
    current = AppVersionPolicy.objects.filter(app=app, platform=platform).first()
    minimum = minimum_version if minimum_version is not None else (
        current.minimum_version if current else '0.0.0')
    latest = latest_version if latest_version is not None else (
        current.latest_version if current else minimum)
    url = store_url if store_url is not None else (current.store_url if current else '')
    msg = message if message is not None else (current.message if current else '')
    url = _validate(app, platform, minimum, latest, url, msg)
    old_min = parse_version(current.minimum_version) if current else (0, 0, 0, 0)
    if parse_version(minimum) > old_min:
        from . import markets

        country = markets.policy_country()
        payload = {'app': app, 'platform': platform, 'minimum_version': minimum,
                   'latest_version': latest, 'store_url': url, 'message': msg}
        outcome = approvals.require_approval_or_execute(
            PLATFORM_APP_VERSION, actor=actor, country=country, scope_node=root,
            target_type=TARGET, target_id=f'{app}:{platform}', payload=payload,
            idempotency_key=f'platform_app_version:{app}:{platform}:{minimum}:{latest}',
            reason=reason, http_request=http_request)
        return current, outcome
    row, _changed = apply_policy(app, platform, minimum, latest, url, msg, actor=actor,
                                 reason=reason, request=http_request)
    return row, None


# ---------------------------------------------------------------------------
# The server-side gate
# ---------------------------------------------------------------------------

def check_request(request, default_app):
    """None when the request may proceed, else `(code, message, details)` for a 426."""
    headers = getattr(request, 'META', None) or {}
    return check_headers(headers.get('HTTP_X_APP_VERSION'), headers.get('HTTP_X_APP_PLATFORM'),
                         headers.get('HTTP_X_APP_NAME'), default_app)


def check_headers(version, platform, app, default_app):
    if not version:
        return None
    parsed = parse_version(version)
    platform = str(platform or '').strip().lower()
    app = str(app or default_app or '').strip().lower()
    if parsed is None or platform not in PLATFORMS or app not in APPS:
        return None
    row = AppVersionPolicy.objects.filter(app=app, platform=platform).first()
    if row is None:
        return None
    minimum = parse_version(row.minimum_version)
    if minimum is None or parsed >= minimum:
        return None
    return (UPDATE_REQUIRED,
            row.message or 'Please update the app to continue.',
            {'minimum_version': row.minimum_version, 'latest_version': row.latest_version,
             'store_url': row.store_url})



def update_required_response(refusal):
    """The stable 426 body for a `check_request` refusal: the standard error envelope, plus the
    minimum, latest and store link under `error.update` so the app can send the user there."""
    from rest_framework import status as http

    from base.utils import error_response

    code, message, details = refusal
    response = error_response(code=code, message=message, field='app_version',
                              issue='This app version is below the minimum supported version',
                              status=http.HTTP_426_UPGRADE_REQUIRED)
    response.data['error']['update'] = details
    return response
