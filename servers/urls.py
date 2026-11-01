from django.urls import path, include

from base.uploads_views import presign_upload
from servers.auth_user import urls as auth_user_urls
from servers.rider import urls as rider_urls
from servers.driver import urls as driver_urls
from servers.ride import urls as ride_urls
from servers.payments import urls as payments_urls
from servers.payments import razorpay_webhooks
from servers.sos import urls as sos_urls
from servers.pricing import urls as pricing_urls
from servers.support import urls as support_urls
from servers.admin_access import urls as admin_access_urls
from servers.approvals import urls as approvals_urls
from servers.command_centre import urls as command_centre_urls
from servers.search import urls as search_urls
from servers.safety import urls as safety_urls
from servers.live_ops import urls as live_ops_urls
from servers.notifications import urls as notifications_urls
from servers.promotions import urls as promotions_urls
from servers.fraud import urls as fraud_urls
from servers.reports import urls as reports_urls
from servers.admin_audit import urls as admin_audit_urls
from servers.platform_ops import urls as platform_ops_urls
from servers.platform_ops.views import app_config as app_config_view

urlpatterns = [
    path('uploads/presign/', presign_upload),
    path('auth/', include(auth_user_urls.urlpatterns)),
    path('rider/', include(rider_urls.urlpatterns)),
    path('driver/', include(driver_urls.urlpatterns)),
    path('ride/', include(ride_urls)),
    path('payments/', include(payments_urls)),
    # Provider webhooks (no JWT; authenticated by signature). See servers/payments/razorpay_webhooks.py
    path('webhooks/razorpay/', razorpay_webhooks.razorpay_webhook),
    path('webhooks/razorpayx/', razorpay_webhooks.razorpayx_webhook),
    path('sos/', include(sos_urls.urlpatterns)),
    path('pricing/', include(pricing_urls)),
    path('support/', include(support_urls)),
    path('admin-access/', include(admin_access_urls.urlpatterns)),
    path('approvals/', include(approvals_urls.urlpatterns)),
    path('command-centre/', include(command_centre_urls.urlpatterns)),
    path('search/', include(search_urls.urlpatterns)),
    path('safety/', include(safety_urls.urlpatterns)),
    path('live-ops/', include(live_ops_urls.urlpatterns)),
    path('notifications/', include(notifications_urls.urlpatterns)),
    path('promotions/', include(promotions_urls.urlpatterns)),
    path('fraud/', include(fraud_urls.urlpatterns)),
    path('reports/', include(reports_urls.urlpatterns)),
    path('admin-audit/', include(admin_audit_urls.urlpatterns)),
    path('platform/', include(platform_ops_urls.urlpatterns)),
    # Public, read-only market config for the rider and driver apps (module 15).
    path('app-config/', app_config_view, name='app_config'),
]
