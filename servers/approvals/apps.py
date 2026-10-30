from django.apps import AppConfig


class ApprovalsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'servers.approvals'
    label = 'approvals'

    def ready(self):
        # Executors register themselves by action key when imported. Importing
        # them here, once, is what makes "approve" able to find the code to
        # run; an executor module nobody imports is a request nobody can
        # approve, and that would only be discovered by the checker.
        from . import adjust, executors  # noqa: F401
        from servers.payments import finance_executors  # noqa: F401  (Part B finance actions)
        from servers.support import sla_change  # noqa: F401  (support.sla_change)
