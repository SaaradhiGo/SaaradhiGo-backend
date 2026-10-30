from django.apps import AppConfig


class RiderConfig(AppConfig):
    name = 'servers.rider'

    def ready(self):
        # The Riders module's bulk runner registers with the bulk.action
        # dispatcher on import; a checker's approve finds it.
        from . import admin_riders  # noqa: F401

        # A block that landed mid-trip signs the rider out when the trip ends.
        from django.db.models.signals import post_save

        from servers.ride.models import Trip

        from .block_signout import on_trip_saved
        post_save.connect(on_trip_saved, sender=Trip,
                          dispatch_uid='rider.block_signout.on_trip_saved')
