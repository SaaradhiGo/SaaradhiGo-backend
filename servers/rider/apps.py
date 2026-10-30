from django.apps import AppConfig


class RiderConfig(AppConfig):
    name = 'servers.rider'

    def ready(self):
        # The Riders module's bulk runner registers with the bulk.action
        # dispatcher on import; a checker's approve finds it.
        from . import admin_riders  # noqa: F401
