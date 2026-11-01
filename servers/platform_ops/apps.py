from django.apps import AppConfig


class PlatformOpsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'servers.platform_ops'
    label = 'platform_ops'

    def ready(self):
        # Executors register by action key when imported (see approvals.registry).
        from . import workflows  # noqa: F401  (platform.city_status, flag_change, ...)
