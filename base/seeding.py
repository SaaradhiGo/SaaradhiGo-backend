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
