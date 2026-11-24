"""Archive a city that is not a planned market, with its test accounts (owner, 24 Nov: QA's Austin).

Only a city with no real completed trips, and only accounts flagged as test accounts are touched:
test drivers and riders whose home city it is are deactivated. Every write is audited to the actor.
"""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from base.test_data import real_trips
from servers.admin_access.models import ARCHIVED, CITY, GeoNode
from servers.admin_audit.actor_option import add_actor_argument, attributed, require_actor
from servers.admin_audit.services import record_admin_action

COMMAND = 'archive_city'


class Command(BaseCommand):
    help = 'Archive a city (never live) and deactivate its test drivers and riders.'

    def add_arguments(self, parser):
        parser.add_argument('code')
        parser.add_argument('--reason', required=True)
        parser.add_argument('--apply', action='store_true')
        add_actor_argument(parser)

    def handle(self, *args, code, reason, apply, **options):
        from servers.ride.models import Trip

        node = GeoNode.objects.filter(code=code.upper(), kind=CITY).first()
        if node is None:
            raise CommandError(f'{code} is not a city')
        if real_trips(Trip.objects.filter(zone__geo_node=node, status_id__status_code='completed')).exists():
            raise CommandError(f'{node.code} has real completed trips: pause it instead')
        users = get_user_model().objects.filter(test_account__isnull=False, is_active=True).filter(
            driver__home_city=node) | get_user_model().objects.filter(
            test_account__isnull=False, is_active=True, rider__home_city=node)
        users = list(users.distinct())
        self.stdout.write(f'{node.name} ({node.status}) -> archived; {len(users)} test account(s) to deactivate')
        if not apply:
            self.stdout.write('Dry run: nothing written. Add --apply.')
            return
        actor = require_actor(options, COMMAND)
        from servers.admin_access.readiness import set_city_status
        from servers.admin_audit.actor_option import actor_label

        with attributed(actor, COMMAND, reason=reason), transaction.atomic():
            set_city_status(node, ARCHIVED, actor, reason, actor_label=actor_label(actor, COMMAND))
            for user in users:
                user.is_active = False
                user.save(update_fields=['is_active'])
                record_admin_action(None, action='test_account_deactivated', target_type='user', target_id=user.pk,
                                    before={'is_active': True}, after={'is_active': False}, reason=reason)
        self.stdout.write(f'Archived {node.name}; deactivated {len(users)} test account(s).')
