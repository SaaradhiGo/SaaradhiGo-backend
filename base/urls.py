from django.contrib import admin
from django.urls import path, include

from base.health import healthz
from servers.urls import urlpatterns as api_urls


urlpatterns = [
    # Health probe — unauthenticated, cheap, used by ALB target-group +
    # external uptime monitors. Listed before /admin/ and /api/ so the
    # probe never trips middleware redirects or route mismatches.
    path('healthz', healthz),
    path('healthz/', healthz),
    path('admin/', admin.site.urls),
    path('api/v1/', include(api_urls)),
]
