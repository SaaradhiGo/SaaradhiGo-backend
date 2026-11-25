from django.apps import AppConfig


class SafetyConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'servers.safety'
    label = 'safety'

    def ready(self):
        # The hourly rota's approval executor registers on import.
        from . import rota  # noqa: F401
