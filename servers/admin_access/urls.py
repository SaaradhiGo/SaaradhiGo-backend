from django.urls import path

from .reveal import reveal_pii
from .views import admin_whoami

urlpatterns = [
    # What the caller holds. Used to draw the console; never trusted on the
    # way back in -- every endpoint enforces its own permission and scope.
    path('me/', admin_whoami, name='admin_whoami'),
    # The one door to unmasked personal data: one record, the fields asked
    # for, `pii.view` at the record's node, one audit row per call.
    path('reveal/', reveal_pii, name='admin_reveal_pii'),
]
