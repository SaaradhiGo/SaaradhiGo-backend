from django.apps import AppConfig


class AdminAuditConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'servers.admin_audit'
    label = 'admin_audit'

    def ready(self):
        # Every write to an access row (assignments, roles, scopes, the
        # authority flags on a user) appends an audit row, whoever makes
        # it. Connected here, once, after every model is loaded.
        from . import access_audit

        access_audit.connect()
