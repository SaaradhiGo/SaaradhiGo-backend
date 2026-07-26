from django.apps import AppConfig


class AuthUserConfig(AppConfig):
    name = 'servers.auth_user'

    def ready(self):
        import servers.auth_user.services
