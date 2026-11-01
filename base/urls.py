from django.urls import path, include
from django.views.defaults import bad_request, permission_denied, page_not_found

from base.config_view import public_config
from base.health import healthz, version
from servers.urls import urlpatterns as api_urls

handler400 = 'base.views.bad_request_view'
handler403 = 'base.views.permission_denied_view'
handler404 = 'base.views.page_not_found_view'

# THE DJANGO CONSOLE IS OFF (owner ruling, AUDIT 27.62). There is no Django sign-in, second-factor
# challenge, sign-out, admin site (`admin/`) or server-rendered console page here: each of those
# paths is simply not routed, so it answers 404 like any unknown URL. The only public surface
# is the API under api/v1/, the health probes and the websockets. Emergency access is the
# console's break-glass account (servers/admin_access) and Railway / manage.py, never a Django page.
# tests/test_django_console_off.py pins every removed route to 404.
urlpatterns = [
    # Health probe — unauthenticated, cheap, used by ALB target-group +
    # external uptime monitors. Listed before /api/ so the
    # probe never trips middleware redirects or route mismatches.
    path('healthz', healthz),
    # Deploy verification: poll this and compare `revision` with the commit you
    # pushed. A 200 from /healthz can come from the OLD container.
    path('version', version),
    path('version/', version),
    path('healthz/', healthz),
    # Public client-config: feature flags the mobile + web apps read at
    # startup. Unauthenticated by design; do NOT add secrets here.
    path('api/v1/config/', public_config),
    path('api/v1/', include(api_urls)),
]
