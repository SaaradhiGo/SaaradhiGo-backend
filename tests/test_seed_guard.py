"""Test data cannot be loaded where it must not go.

Four commands create accounts with fixed phone numbers and passwords. They
share one gate, and this proves the gate — by command, not by helper, so a
command that forgets to call it fails here.
"""

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

pytestmark = pytest.mark.django_db

SEEDS = [
    ('bootstrap_qa_accounts', []),
    ('bootstrap_qa_operator', []),
    ('seed_load_fixtures', []),
    ('seed_admin_access', ['--test-admins']),
]


@pytest.mark.parametrize('command,args', SEEDS)
def test_refused_in_production(command, args, settings):
    settings.IS_PRODUCTION = True
    settings.TEST_PHONE_NUMBERS = {'+919999000001': '100001'}

    with pytest.raises(CommandError, match='production'):
        call_command(command, *args)


@pytest.mark.parametrize('command,args', SEEDS)
def test_refused_when_otp_gating_is_off(command, args, settings):
    """The seeded numbers are a real-looking range; without the gate they
    would receive real one-time codes."""
    settings.IS_PRODUCTION = False
    settings.TEST_PHONE_NUMBERS = {}

    with pytest.raises(CommandError, match='TEST_PHONE_NUMBERS'):
        call_command(command, *args)


def test_geo_and_roles_still_seed_in_production(settings):
    """Configuration is not test data. The admin scope tree and the role
    templates must load in production; only --test-admins is gated."""
    from servers.admin_access.models import AdminRole, GeoNode

    from conftest import seed_actor

    actor = seed_actor()
    settings.IS_PRODUCTION = True
    settings.TEST_PHONE_NUMBERS = {}

    call_command('seed_admin_access', actor_email=actor.email)

    assert GeoNode.objects.exists() and AdminRole.objects.exists()
