from django.apps import AppConfig


class AdminAuditConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'servers.admin_audit'
    label = 'admin_audit'
