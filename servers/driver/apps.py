from django.apps import AppConfig


class DriverConfig(AppConfig):
    name = 'servers.driver'

    def ready(self):
        # The Drivers module's deactivation and bulk executors register with
        # the approvals registry on import; a checker's approve finds them.
        from . import admin_drivers  # noqa: F401
