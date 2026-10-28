from django.apps import AppConfig


class AuthUserConfig(AppConfig):
    name = 'servers.auth_user'

    def ready(self):
        import servers.auth_user.services

        # A user who appears as an audit actor, or who is staff, is never
        # deleted (owner ruling, 2026-10-27). Connected here, once, so the
        # rule holds for every ORM path and not only for callers who know.
        from . import erasure

        erasure.connect()
