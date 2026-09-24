from django.contrib import admin
from django.urls import path, include
from django.views.defaults import bad_request, permission_denied, page_not_found

from base.config_view import public_config
from base.health import healthz, version
from servers.urls import urlpatterns as api_urls
from servers.admin_dashboard.urls import urlpatterns as admin_urls

handler400 = 'base.views.bad_request_view'
handler403 = 'base.views.permission_denied_view'
handler404 = 'base.views.page_not_found_view'

urlpatterns = [
    # Health probe — unauthenticated, cheap, used by ALB target-group +
    # external uptime monitors. Listed before /admin/ and /api/ so the
    # probe never trips middleware redirects or route mismatches.
    path('healthz', healthz),
    # Deploy verification: poll this and compare `revision` with the commit you
    # pushed. A 200 from /healthz can come from the OLD container.
    path('version', version),
    path('version/', version),
    path('healthz/', healthz),
    path('admin/', admin.site.urls),
    # Public client-config: feature flags the mobile + web apps read at
    # startup. Unauthenticated by design; do NOT add secrets here.
    path('',include(admin_urls)),
    path('api/v1/config/', public_config),
    path('api/v1/', include(api_urls)),
]
