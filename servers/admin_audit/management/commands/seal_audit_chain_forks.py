"""Seal audit-chain forks written before the 22 Nov locking fix.

    python manage.py seal_audit_chain_forks                       # dry run: what would be sealed
    python manage.py seal_audit_chain_forks --confirm \\
        --actor-email you@saaradhigo.com --reason "..."

WHAT HAPPENED
-------------
Until 22 Nov two audit rows written at the same moment could both link to
the same predecessor (see `AdminAuditLog._link_into_chain`). `verify_audit_chain`
then reports the second as a "broken link" forever: on QA, 145 of them across
3,423 rows, every one a pair written milliseconds apart.

WHAT THIS DOES
--------------
Nothing is rewritten. It refuses unless EVERY problem is a fork -- a broken
link whose stored previous hash is the hash of an earlier row -- and no row's
content is altered. Then it appends one row that names the fork ids and
carries a digest of every row up to it. The verifier accepts those ids
linking to an earlier row, and the digest still exposes any later edit,
removal or insertion among the sealed rows. Anything else stays a failure.
"""

from django.core.management.base import BaseCommand, CommandError

from servers.admin_audit.actor_option import actor_label, add_actor_argument, require_actor
from servers.admin_audit.hashchain import ChainStateError, inspect_chain, write_fork_seal
from servers.admin_audit.models import AdminAuditLog

COMMAND = 'seal_audit_chain_forks'
MIN_REASON = 20


class Command(BaseCommand):
    help = 'Seal audit-chain forks from concurrent writes before the lock fix.'

    def add_arguments(self, parser):
        parser.add_argument('--confirm', action='store_true')
        parser.add_argument('--reason', default='')
        add_actor_argument(parser)

    def handle(self, *args, **options):
        report = inspect_chain(AdminAuditLog.objects.all())
        problems = report['problems']
        kinds = {}
        for p in problems:
            kinds[p['issue']] = kinds.get(p['issue'], 0) + 1
        self.stdout.write(f'{report["checked"]} row(s), {len(problems)} problem(s): {kinds or "none"}')
        if not options['confirm']:
            self.stdout.write('Dry run. Add --confirm --actor-email --reason to seal.')
            return
        reason = (options['reason'] or '').strip()
        if len(reason) < MIN_REASON:
            raise CommandError(f'--reason must say why, at least {MIN_REASON} characters.')
        actor = require_actor(options, COMMAND)
        try:
            row = write_fork_seal(AdminAuditLog, label=actor_label(actor, COMMAND), reason=reason, actor=actor)
        except ChainStateError as exc:
            raise CommandError(str(exc)) from exc
        after = inspect_chain(AdminAuditLog.objects.all())
        self.stdout.write(f'Sealed {row.after["forks"]} fork(s) in row {row.pk}. '
                          f'Chain now: {len(after["problems"])} problem(s).')
        if after['problems']:
            raise CommandError('The chain still reports problems after sealing.')
