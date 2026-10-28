"""The response post-processor: every operator response is masked here.

WHY A RENDERER AND NOT A CALL IN EACH VIEW
------------------------------------------
Masking inside each view protects the views somebody remembered. The
driver list was masked; the trip list, the payout queue's UPI handle, the
ticket thread's author names and the rider list were not, because nobody
had added the call there yet. A new endpoint tomorrow starts unmasked by
default. That is the wrong default for personal data.

So the masking happens where every DRF response passes on its way out: the
renderer. `MaskingJSONRenderer` is the project's default JSON renderer. For
a response produced under the operator namespace it masks the payload by
field name (`masking.mask_payload`) for the policy the caller holds, and
only then serializes. A view cannot forget to be masked. The only way to
send a real value is `masking.mark_revealed(request)`, which the reveal
endpoints call after writing their audit row.

WHICH RESPONSES COUNT AS "OPERATOR"
-----------------------------------
The same rule `tests/test_admin_namespace_is_gated.py` applies to routes:
a path under `api/v1/` with an `admin/` segment, or under
`api/v1/admin-access/`; plus any view gated on `ScopedPermission`, so an
alias path to an admin view is masked as that view. Rider and driver
endpoints never match, and their contracts (`contracts/*.json`) are
untouched -- a rider reading their own profile is not an operator reading
somebody else's.

`SELF_DESCRIBING` lists the two operator routes whose payload is the
caller's OWN account (sign-in, and `/admin-access/me/`): masking an
operator's own email on the page that greets them by it would be theatre.
Each entry carries a reason and the guard test pins the size.
"""

import re

from rest_framework.renderers import JSONRenderer

from .masking import is_revealed, mask_payload, policy_for

ADMIN_SEGMENT = re.compile(r'(^|/)admin/')

#: Operator routes whose body describes the CALLER and nobody else.
SELF_DESCRIBING = {
    '/api/v1/auth/admin/login/': 'Returns the operator who just signed in, to themselves.',
    '/api/v1/admin-access/me/': 'Returns the caller’s own roles, scopes and email.',
}


def is_operator_path(path):
    if path.startswith('/api/v1/admin-access/'):
        return True
    return path.startswith('/api/v1/') and bool(ADMIN_SEGMENT.search(path))


def _view_is_scope_gated(view):
    from .bridge import ScopedPermission

    classes = getattr(view, 'permission_classes', None) or ()
    return any(isinstance(p, ScopedPermission) for p in classes)


def is_operator_response(request, view):
    """Should this response be masked?

    Decided from the path AND the view's gate, so neither an alias route
    nor a view mounted somewhere unexpected escapes.
    """
    if request is None:
        return False
    path = getattr(request, 'path', '') or ''
    if path in SELF_DESCRIBING:
        return False
    return is_operator_path(path) or _view_is_scope_gated(view)


class MaskingJSONRenderer(JSONRenderer):
    """DRF's JSON renderer, masking operator payloads before serializing."""

    def render(self, data, accepted_media_type=None, renderer_context=None):
        context = renderer_context or {}
        request = context.get('request')
        view = context.get('view')
        if (
            data is not None
            and is_operator_response(request, view)
            and not is_revealed(request)
        ):
            user = getattr(request, 'user', None)
            data = mask_payload(data, policy_for(user))
        return super().render(data, accepted_media_type, renderer_context)
