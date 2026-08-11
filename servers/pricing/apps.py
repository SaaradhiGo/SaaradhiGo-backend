from django.apps import AppConfig


class PricingConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'servers.pricing'
    label = 'pricing'
    verbose_name = 'Pricing & Service Zones'

    def ready(self):
        # Wire up signals (polygon cache invalidation on zone save/delete).
        from servers.pricing import signals  # noqa: F401
