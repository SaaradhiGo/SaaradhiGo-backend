from django.apps import AppConfig


class CommandCentreConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'servers.command_centre'
    label = 'command_centre'

    def ready(self):
        # The threshold-change executor registers itself with the approvals
        # registry on import. Importing it here is what lets a checker's
        # approve find the code to run; without it a request could be
        # raised that nobody can ever carry out.
        from . import thresholds  # noqa: F401
