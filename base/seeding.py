"""One gate for every command that loads test accounts or test data.

WHY A SHARED GUARD
------------------
Four commands create accounts with fixed phone numbers and passwords:
`bootstrap_qa_accounts`, `bootstrap_qa_operator`, `seed_load_fixtures` and
`seed_admin_access --test-admins`. Each had its own environment check, and
each spelled it slightly differently. One of them is one refactor away from
spelling it wrong, and the place that would notice is production.

WHAT "SEEDABLE" MEANS
---------------------
Two things, both required:

* Not production. `IS_PRODUCTION` is the same signal the settings boot guards
  use, so there is one definition of the word.
* OTP gating is ON. The seeded phone numbers are a plausible Indian mobile
  range -- India has no reserved block for fiction -- so they are safe only
  while `TEST_PHONE_NUMBERS` maps them to fixed codes and the real SMS path
  never sees them. An environment with that gate empty would send one-time
  codes to whoever actually holds those numbers. Seeding there is refused.

A seed command calls `require_seedable_environment` before it writes
anything. It raises `CommandError`, which Django prints and exits non-zero
with, so a deploy script that runs a seed by mistake fails loudly.
"""

from django.conf import settings
from django.core.management.base import CommandError
from django.db import IntegrityError, transaction


class NotSeedable(CommandError):
    """Raised instead of loading test data somewhere it must not go."""


def require_seedable_environment(what='test data'):
    """Refuse unless this environment may hold test accounts.

    `what` names the thing being refused, for the message: a person reading
    a failed deploy log should not have to open the command to learn what
    it was about to do.
    """
    if getattr(settings, 'IS_PRODUCTION', False):
        raise NotSeedable(
            f'Refusing to load {what}: this is production '
            f'(ENVIRONMENT={getattr(settings, "ENVIRONMENT", "?")!r}). '
            f'Test accounts never belong here.'
        )

    if not getattr(settings, 'TEST_PHONE_NUMBERS', None):
        raise NotSeedable(
            f'Refusing to load {what}: TEST_PHONE_NUMBERS is empty, so OTP '
            f'gating is off. The seeded phone numbers are a real-looking '
            f'range and would receive real one-time codes. Set '
            f'TEST_PHONE_NUMBERS for this environment first.'
        )


# ---------------------------------------------------------------------------
# Seeds create accounts; they never change one (owner, AUDIT 27.40)
# ---------------------------------------------------------------------------
#
# A seed or bootstrap command runs on a deploy or by hand, often with nobody
# watching. An account that already exists belongs to whoever is using it: its
# password, usable-password state, roles, `ops_role`, `is_staff`,
# `is_superuser`, assignments, second factor, recovery codes and break-glass
# flag are THEIRS. So the only way a seed may write an account is the helper
# below, which looks first and returns an existing row UNTOUCHED; it has no
# path that calls `save()` on a row that was already there.
#
# This is enforced structurally, not by discipline:
#   * `tests/test_seed_never_modifies_accounts.py` parses every `seed_*` and
#     `bootstrap*` command and fails on `set_password`, `set_unusable_password`,
#     a `save()` of an account variable, or an `update()` / `update_or_create()`
#     / `delete()` on the user, assignment or second-factor models;
#   * the same file runs EACH seed twice over accounts in every role (a
#     break-glass account with MFA and recovery codes among them) and asserts
#     every auth-relevant column and related row is unchanged;
#   * a seed command nobody registered there fails the run.
#
# Role TEMPLATES (`AdminRole.permissions`) are not accounts: `seed_admin_access`
# keeps updating them, and says so in its docstring.


def get_or_create_account(model, *, lookup, defaults=None, password=None):
    """`(account, created)`. NEVER writes a row that already exists.

    `lookup` finds the account (an email, a phone); `defaults` are the fields
    of a NEW one. A new account gets `password` when one is given, else NO
    usable password. An existing account is returned exactly as it is: no
    field, no password, no `save()`.
    """
    existing = model.objects.filter(**lookup).first()
    if existing is not None:
        return existing, False
    account = model(**lookup, **(defaults or {}))
    if password:
        account.set_password(password)
    else:
        account.set_unusable_password()
    try:
        with transaction.atomic():
            account.save()
    except IntegrityError:
        # Created by a concurrent run between the look and the write.
        existing = model.objects.filter(**lookup).first()
        if existing is None:
            raise
        return existing, False
    return account, True


def create_assignment_if_missing(admin, role, scope, reason=''):
    """`(assignment, created)`. An existing assignment (active or not) is
    returned untouched: a seed never reactivates, deactivates or edits one."""
    from servers.admin_access.models import AdminAssignment

    existing = AdminAssignment.objects.filter(admin=admin, role=role, scope=scope).first()
    if existing is not None:
        return existing, False
    return AdminAssignment.objects.create(admin=admin, role=role, scope=scope, reason=reason), True
