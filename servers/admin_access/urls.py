from django.urls import path

from .views import admin_whoami

urlpatterns = [
    # What the caller holds. Used to draw the console; never trusted on the
    # way back in -- every endpoint enforces its own permission and scope.
    path('me/', admin_whoami, name='admin_whoami'),
]
