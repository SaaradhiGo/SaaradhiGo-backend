from django.apps import AppConfig


class PromotionsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'servers.promotions'
    label = 'promotions'

    def ready(self):
        # Importing registers the maker-checker executors (promotion.launch, budget_increase).
        from . import workflow  # noqa: F401
