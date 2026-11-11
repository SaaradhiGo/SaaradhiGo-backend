"""`--actor-email`: the person behind a management command that writes.

THE RULING (owner, 2026-10-28)
------------------------------
    --actor-email is mandatory for migrate_operators --apply and every other
    management command that writes access, money or driver status. It must
    resolve to an active operator; refuse otherwise.

WHY A SHARED HELPER AND NOT A PATTERN
-------------------------------------
Ten commands write one of those three things. Each could add its own flag
and its own lookup, and each would spell the rule slightly differently: one
would forget `is_active`, one would accept a rider with the right address,
one would let the shared emergency account sign a routine change. The place
that would notice is the access review. So the rule lives here, once, and
`tests/test_actor_email_mandatory.py` enumerates every command module in
the repository and fails if a command that writes does not import this.

WHAT "RESOLVES TO AN ACTIVE OPERATOR" MEANS
-------------------------------------------
Exactly one account whose email matches, case-insensitively, and which is:

  * active -- a deactivated account cannot act, so it cannot be the actor;
  * an operator by the one definition (`base.permissions.is_operator`):
    `role='admin'` with `is_staff`, or a superuser;
  * NOT the break-glass account. That account is a shared emergency
    credential that nobody owns; its sign-in demands a reason and alerts
    every Super Admin precisely because it is not a person. A routine
    command attributed to it is a row that names nobody, and the whole
    point of the flag is a row that names somebody.

Anything else is refused with a message that says which test failed, so
the person at the terminal knows whether to fix the address or go and get
the right account.

DRY RUNS DO NOT NEED IT
-----------------------
`--actor-email` is optional at parse time so that `migrate_operators`
without `--apply`, `recompute_driver_trip_counts` without `--apply` and
`reset_operator_mfa` without `--confirm` keep working as the harmless
rehearsal they are meant to be. A command calls `require_actor` on its
WRITE path only, and before its first write.

UNATTENDED RUNS
---------------
Railway's QA start command runs `bootstrap_qa_accounts` on every boot, and
`seed_admin_access` is documented as safe to run on every deploy. A deploy
cannot type an email address. For those commands the fallback is the
setting `QA_BOOTSTRAP_ACTOR_EMAIL` (an environment variable on the QA
service), which names the operator the deploy's writes are attributed to
and is validated by exactly the same rule -- it is a way to supply the
address, not a way around the check. The fallback is IGNORED in production:
a production deploy that seeds access rows unattended is the thing that
must not exist, so there a person passes the flag.

THE FIRST OPERATOR (owner, 2026-10-28)
--------------------------------------
    Allowed only while zero active operators exist. The account it creates
    is audited with actor 'system:genesis', and the exemption closes the
    moment one active operator exists.

An empty database has no operator to name. `create_operator` and the two
QA bootstraps are the only commands that can create one, and each of them
would otherwise be impossible to run for the first time. So, for those
three only: when NO active operator exists at all, the command may run
without an actor; its rows carry `actor_id` NULL, the label
`system:genesis` (`GENESIS_LABEL`) and `after.genesis: true`. The moment
one ACTIVE operator exists the exemption is gone -- the second operator is
created by the first, with a name attached.

"One active operator exists" is `any_operator_exists`: an account that is
active and operator-shaped (`role='admin'` with `is_staff`, or a
superuser). A deactivated operator does not count: nobody can sign in as
it, so it cannot create the next one. The break-glass account DOES count
when it is active: it is an operator somebody can sign in as (with a
reason) and then use to run `create_operator --actor-email` -- the row
would name the emergency account, which is the loud record wanted, rather
than nobody. Closing the exemption as soon as any usable operator exists
is the conservative reading of "zero active operators", and
`tests/test_actor_email_mandatory.py::TestGenesis` holds both cases.
"""

from contextlib import contextmanager

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import CommandError
from django.db.models import Q

from base.permissions import is_operator

from .context import audit_context

OPTION = '--actor-email'
DEST = 'actor_email'
#: The setting (an environment variable on the QA service) that names the
#: actor for a command a deploy runs without a person at the keyboard.
UNATTENDED_SETTING = 'QA_BOOTSTRAP_ACTOR_EMAIL'
#: The actor label on every row a genesis run writes. Exactly this, by the
#: owner's ruling: a reader of the audit log greps for one string.
GENESIS_LABEL = 'system:genesis'


class ActorRefused(CommandError):
    """`--actor-email` did not resolve to an account that may act."""


def add_actor_argument(parser, *, required=False, help=None):
    """Add `--actor-email` to a command's parser.

    Optional at parse time by default, so a dry run stays usable without
    it; the write path enforces it through `require_actor`. A command whose
    every run writes (`record_retroactive_access_audit`) passes
    `required=True` so argparse refuses before `handle` starts.
    """
    parser.add_argument(
        OPTION, dest=DEST, default='', required=required,
        help=help or (
            'Email of the operator running this. Required for any run that '
            'writes; every audit row the run appends names them. Must be an '
            'active operator account and not the break-glass account.'
        ),
    )


def resolve_actor(email, *, source=OPTION):
    """The operator account behind `email`, or raise `ActorRefused`.

    `source` names where the address came from (`--actor-email` or the
    unattended setting) so the refusal says what to fix.
    """
    address = (email or '').strip()
    if not address:
        raise ActorRefused(f'{source} is empty.')

    user = get_user_model().objects.filter(email__iexact=address).first()
    if user is None:
        raise ActorRefused(
            f'{source} {address!r} matches no account. The actor on an audit '
            f'row must be a real operator; create them first with '
            f'create_operator.'
        )
    if not user.is_active:
        raise ActorRefused(
            f'{source} {address!r} is a deactivated account (user id '
            f'{user.pk}). A deactivated account cannot act, so it cannot be '
            f'the actor of a change.'
        )
    if not is_operator(user):
        raise ActorRefused(
            f'{source} {address!r} is not an operator (user id {user.pk}: it '
            f'needs role=admin and is_staff, or superuser). A rider or driver '
            f'account cannot be the actor of an operator change.'
        )
    if getattr(user, 'is_break_glass', False):
        raise ActorRefused(
            f'{source} {address!r} is the break-glass account (user id '
            f'{user.pk}). It is a shared emergency credential that nobody '
            f'owns, so a routine command attributed to it names nobody. Run '
            f'this as yourself.'
        )
    return user


def any_operator_exists():
    """Is there at least one active operator-shaped account?

    Active and operator-shaped, nothing more: an active break-glass
    account counts (see the module docstring), a deactivated Super Admin
    does not.
    """
    return get_user_model().objects.filter(is_active=True).filter(
        Q(is_superuser=True) | Q(role='admin', is_staff=True)
    ).exists()


def require_actor(options, command, *, unattended=False, allow_genesis=False):
    """The operator this run is attributed to. Call before the first write.

    `options` is the command's parsed options dict. `command` is the
    command's name, for the messages.

    `unattended=True` lets `QA_BOOTSTRAP_ACTOR_EMAIL` stand in for the flag
    outside production. `allow_genesis=True` lets the run proceed with NO
    actor when the database holds no active operator at all, returning
    None; the caller labels its rows with `genesis_label`.
    """
    email = (options.get(DEST) or '').strip()
    source = OPTION

    if not email and unattended and not getattr(settings, 'IS_PRODUCTION', False):
        email = (getattr(settings, UNATTENDED_SETTING, '') or '').strip()
        source = UNATTENDED_SETTING

    if email:
        return resolve_actor(email, source=source)

    if allow_genesis and not any_operator_exists():
        return None

    hint = ''
    if unattended:
        hint = (
            f' An unattended run (a deploy) names the actor through the '
            f'{UNATTENDED_SETTING} environment variable instead'
            + ('; that variable is ignored in production, where a person '
               'passes the flag.' if getattr(settings, 'IS_PRODUCTION', False)
               else '.')
        )
    raise ActorRefused(
        f'{command} writes and needs {OPTION} <address of the operator '
        f'running it>. Nothing was changed. The address must be an active '
        f'operator account and not the break-glass account.{hint}'
    )


def actor_label(actor, command):
    """`<email> via <command>`: survives the actor's deletion."""
    if actor is None:
        return genesis_label(command)
    return f'{actor.email} via {command}'[:255]


def genesis_label(command=None):
    """The label on a genesis row: `system:genesis`, whichever command.

    `command` is accepted so callers read the same as `actor_label`, and
    ignored: the ruling fixes the string, and the command's name is already
    on the row's `reason`.
    """
    return GENESIS_LABEL


@contextmanager
def attributed(actor, command, *, reason='', action='', extra_after=None):
    """`audit_context` with the actor and the label this module defines.

    Every structural row and every `record_admin_action` call inside the
    block carries the actor and `<email> via <command>`.
    """
    with audit_context(
        actor=actor, actor_label=actor_label(actor, command),
        reason=reason, action=action, extra_after=extra_after,
    ) as ctx:
        yield ctx
