from django.apps import AppConfig


class RideConfig(AppConfig):
    name = 'servers.ride'

    def ready(self):
        # The Trips module's refund, adjustment and bulk executors register
        # themselves with the approvals registry on import. Importing them
        # here is what lets a checker's approve find the code to run.
        from . import admin_trips  # noqa: F401
        # 24 Nov (owner): every new trip, driver, rider, ticket, payout, approval and fraud flag
        # gets a readable reference (base.public_ref).
        from base import public_ref
        public_ref.connect()
