"""Per-operator cap on console search.

Search is a read and writes no audit row, so the only thing standing between
an operator's session token and a script that walks every phone prefix is
this. Sixty searches a minute, per operator, per result type: generous for a
person typing (the console debounces at 250 ms and fans one keystroke out to
four endpoints), and far below the rate at which enumeration is worth doing.

Keyed on the operator, not the address: operators share office and carrier
addresses, and a per-IP bucket would lock a whole desk out when one of them
is busy. The bucket is per ROUTE (`trips`, `drivers`, ...) so one search is
one debit on each, not four on one.
"""

from rest_framework.throttling import UserRateThrottle


class AdminSearchThrottle(UserRateThrottle):
    scope = 'admin_search'

    def get_cache_key(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return None  # the permission class refuses them first
        route = getattr(getattr(request, 'resolver_match', None), 'url_name', None) or 'search'
        return self.cache_format % {
            'scope': f'{self.scope}:{route}',
            'ident': request.user.pk,
        }
