"""A seed or bootstrap command creates accounts; it never changes one.

THE RULING (owner, AUDIT 27.40)
-------------------------------
Every seed or bootstrap command must be unable to MODIFY an existing account:
its password, its usable-password state, its roles and `ops_role`, `is_staff`
and `is_superuser`, its AdminAssignments (a missing one may be created; an
existing one is never deactivated or altered), its MFA devices and recovery
codes, its break-glass flag. A NEW account is still created (the test admins
with no usable password).

HOW IT IS ENFORCED, NOT REMEMBERED
----------------------------------
Mechanism: ONE shared write path plus two independent tests.

* `base.seeding.get_or_create_account` looks first and returns an existing row
  untouched; it has no path that saves a row that was already there.
  `create_assignment_if_missing` is the same for assignments.
* STATIC (`test_no_seed_command_writes_an_account_except_through_the_helper`):
  every `seed_*` and `bootstrap*` command module is parsed. It fails on
  `set_password` / `set_unusable_password` / `make_password`, on `.save()` of
  a variable that holds an account, on assigning a security field
  (`password`, `is_staff`, `is_superuser`, `ops_role`, `role`, `is_active`,
  `is_break_glass`) of one, and on `update()` / `update_or_create()` /
  `bulk_update()` / `delete()` on the user, assignment or second-factor
  models. `test_the_static_guard_bites` proves it by feeding it a violation.
* BEHAVIOURAL (`test_running_every_seed_twice_changes_no_account`): accounts
  in every role, a break-glass account with MFA and recovery codes, and
  accounts sitting exactly where each seed would create one (same email,
  same phone), including a deactivated assignment. EACH seed is run twice
  with `--actor-email` and its usual flags (`--test-admins`); the auth-relevant
  columns and related rows of every pre-existing account must be identical
  after the first run, and the whole snapshot identical after the second.
* DISCOVERY: commands are found by NAME PATTERN. A new `seed_*` or
  `bootstrap*` command that nobody registered below fails
  `test_every_seed_command_is_registered`.

Role TEMPLATES (`AdminRole.permissions`) are NOT accounts: `seed_admin_access`
keeps updating them, and says so in its docstring.

OUT OF SCOPE, AND WHY (`EXCLUDED`)
----------------------------------
Commands whose whole purpose is to change one named account, on purpose, with
a named actor and a reason. They are not seeds and are not discovered by the
pattern; they are listed so a stale or renamed entry fails.
"""

import ast
import importlib.util
import re
from io import StringIO
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command

from servers.admin_access.models import AdminAssignment, AdminRole, GeoNode
from servers.admin_access.roles import ROLE_TEMPLATES

User = get_user_model()

pytestmark = pytest.mark.django_db

ROOT = Path(__file__).resolve().parent.parent
COMMAND_DIRS = sorted((ROOT / 'servers').glob('*/management/commands')) + sorted(
    (ROOT / 'base').glob('management/commands'))
SEED_PATTERN = re.compile(r'^(seed_|bootstrap)')

#: Commands that change ONE NAMED account on purpose. Not seeds.
EXCLUDED = {
    'set_operator_password': 'sets one named operator password, with an actor and a reason',
    'create_operator': 'creates one named operator (the first, or the next) with a temporary password',
    'enroll_operator_mfa': 'enrols one named operator second factor from a shell',
    'reset_operator_mfa': 'removes one named operator second factor, with actor, reason and confirmation',
    'anonymise_user': 'erases one named account (DPDP request)',
    'migrate_operators': 'turns a reviewed, named plan into assignments, only with --apply',
    'qa_simulate_driver_eligibility': 'flips one named driver QA flag; touches no credential',
    'record_retroactive_access_audit': 'writes audit rows only',
    'record_retroactive_linking_audit': 'writes audit rows only',
    'list_admin_access': 'read only',
    'check_geo_links': 'read only',
}


def command_files():
    found = {}
    for directory in COMMAND_DIRS:
        for path in directory.glob('*.py'):
            if path.name != '__init__.py':
                found[path.stem] = path
    return found


def seed_commands():
    return {name: path for name, path in command_files().items() if SEED_PATTERN.match(name)}


# ---------------------------------------------------------------------------
# Static guard
# ---------------------------------------------------------------------------

PASSWORD_CALLS = {'set_password', 'set_unusable_password', 'make_password'}
WRITE_CALLS = {'update', 'update_or_create', 'bulk_update', 'delete'}
ACCOUNT_MODELS = re.compile(
    r'\b(User|user_model|customUser|get_user_model|AdminAssignment|TOTPDevice|StaticDevice|'
    r'StaticToken|TOTP|Static)\b')
ACCOUNT_VARS = {'user', 'account', 'admin', 'operator', 'u', 'owner', 'actor', 'person', 'staff'}
SECURITY_FIELDS = {'password', 'is_staff', 'is_superuser', 'ops_role', 'role', 'is_active',
                   'is_break_glass', 'must_change_password', 'email_verified_at'}


def violations(source, filename='<command>'):
    """Every account write in `source` that does not go through the helper."""
    tree = ast.parse(source, filename)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            attr, receiver = node.func.attr, ast.unparse(node.func.value)
            if attr in PASSWORD_CALLS:
                found.append((node.lineno, f'{receiver}.{attr}()'))
            elif attr in WRITE_CALLS and ACCOUNT_MODELS.search(receiver):
                found.append((node.lineno, f'{receiver}.{attr}()'))
            elif attr == 'save' and receiver in ACCOUNT_VARS:
                found.append((node.lineno, f'{receiver}.save()'))
        elif isinstance(node, (ast.Assign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if (isinstance(target, ast.Attribute) and target.attr in SECURITY_FIELDS
                        and ast.unparse(target.value) in ACCOUNT_VARS):
                    found.append((node.lineno, f'{ast.unparse(target)} = ...'))
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id in PASSWORD_CALLS:
            found.append((node.lineno, f'{node.func.id}()'))
    return found


def test_the_walk_finds_the_seed_commands():
    found = set(seed_commands())
    assert {'seed_admin_access', 'seed_us_test_city', 'bootstrap_qa_accounts',
            'bootstrap_qa_operator', 'seed_load_fixtures', 'seed_safety_config'} <= found


def test_no_seed_command_writes_an_account_except_through_the_helper():
    problems = {}
    for name, path in sorted(seed_commands().items()):
        bad = violations(path.read_text(encoding='utf-8'), str(path))
        if bad:
            problems[name] = bad
    assert problems == {}, (
        'A seed or bootstrap command must create accounts through '
        'base.seeding.get_or_create_account and never change one: ' + repr(problems))


def test_the_static_guard_bites():
    planted = (
        "def handle(self):\n"
        "    user = User.objects.get(email='a@b.c')\n"
        "    user.set_unusable_password()\n"
        "    user.is_staff = True\n"
        "    user.save()\n"
        "    AdminAssignment.objects.filter(admin=user).update(is_active=False)\n"
        "    User.objects.update_or_create(email='x', defaults={})\n"
    )
    kinds = {what for _line, what in violations(planted)}
    assert {'user.set_unusable_password()', 'user.is_staff = ...', 'user.save()',
            'AdminAssignment.objects.filter(admin=user).update()',
            'User.objects.update_or_create()'} <= kinds


def test_the_excluded_commands_still_exist_and_are_not_seeds():
    files = command_files()
    for name, why in EXCLUDED.items():
        assert why.strip()
        assert name in files, f'{name} is excluded but no longer exists; remove it'
        assert not SEED_PATTERN.match(name), f'{name} matches the seed pattern'


def test_the_helper_never_saves_an_existing_row():
    from base.seeding import create_assignment_if_missing, get_or_create_account

    user = User.objects.create_user(phone_number='+919000000001', username='+919000000001',
                                    email='h@saaradhigo.test', password='keep-me-12345')
    before = User.objects.filter(pk=user.pk).values().get()

    same, created = get_or_create_account(
        User, lookup={'email': 'h@saaradhigo.test'},
        defaults={'is_staff': True, 'is_superuser': True, 'role': 'admin'}, password='other-other-1')

    assert created is False and same.pk == user.pk
    assert User.objects.filter(pk=user.pk).values().get() == before

    new, created = get_or_create_account(
        User, lookup={'email': 'n@saaradhigo.test'},
        defaults={'phone_number': '+919000000002', 'username': '+919000000002'})
    assert created is True and not new.has_usable_password()
    assert callable(create_assignment_if_missing)


# ---------------------------------------------------------------------------
# Behavioural guard
# ---------------------------------------------------------------------------

ACTOR_EMAIL = 'seed.actor@saaradhigo.test'

#: command -> kwargs for `call_command` (the actor is added where it is accepted).
REGISTRY = {
    'seed_admin_access': {'test_admins': True},
    'seed_us_test_city': {},
    'seed_safety_config': {},
    'seed_approval_policies': {},
    'seed_alert_thresholds': {},
    'seed_support_sla': {},
    'seed_payout_config': {},
    'seed_load_fixtures': {'riders': 3, 'drivers': 3},
    'bootstrap_qa_accounts': {},
    'bootstrap_qa_operator': {},
}
#: Run order: the geo tree first, the rest after it.
ORDER = ['seed_admin_access', 'seed_us_test_city', 'seed_safety_config', 'seed_approval_policies',
         'seed_alert_thresholds', 'seed_support_sla', 'seed_payout_config', 'seed_load_fixtures',
         'bootstrap_qa_accounts', 'bootstrap_qa_operator']


def test_every_seed_command_is_registered():
    """A new seed added later without registration fails here."""
    assert set(seed_commands()) == set(REGISTRY), (
        'Register every seed_*/bootstrap* command in REGISTRY (and ORDER) so the '
        'behavioural guard runs it twice: ' + repr(set(seed_commands()) ^ set(REGISTRY)))
    assert sorted(ORDER) == sorted(REGISTRY)


def accepts_actor(name):
    source = seed_commands()[name].read_text(encoding='utf-8')
    return 'add_actor_argument' in source


@pytest.fixture
def seed_env(monkeypatch, settings):
    settings.IS_PRODUCTION = False
    settings.ENVIRONMENT = 'load'      # seed_load_fixtures only runs on the load stack
    settings.TEST_PHONE_NUMBERS = {'+919999000001': '100001'}
    monkeypatch.setenv('ENVIRONMENT', 'qa')
    monkeypatch.setenv('DEBUG_ENV', 'False')
    for prefix, phone, email in (
        ('QA_ADMIN', '+919777000001', 'qa.owner@saaradhigo.test'),
        ('QA_RIDER', '+919777000002', 'qa.rider@saaradhigo.test'),
        ('QA_DRIVER', '+919777000003', 'qa.driver@saaradhigo.test'),
    ):
        monkeypatch.setenv(f'{prefix}_BOOTSTRAP_PHONE', phone)
        monkeypatch.setenv(f'{prefix}_BOOTSTRAP_EMAIL', email)
        monkeypatch.setenv(f'{prefix}_BOOTSTRAP_PASSWORD', 'bootstrap-bootstrap-1')


def snapshot():
    """The auth-relevant columns and related rows of every account."""
    from django_otp.plugins.otp_static.models import StaticDevice, StaticToken
    from django_otp.plugins.otp_totp.models import TOTPDevice

    return {
        'users': {r['id']: r for r in User.objects.order_by('id').values()},
        'assignments': {r['id']: r for r in AdminAssignment.objects.order_by('id').values()},
        'totp': {r['id']: r for r in TOTPDevice.objects.order_by('id').values()},
        'static': {r['id']: r for r in StaticDevice.objects.order_by('id').values()},
        'tokens': {r['id']: r for r in StaticToken.objects.order_by('id').values()},
    }


def restricted(snap, baseline):
    """`snap` limited to the rows that existed in `baseline`."""
    return {kind: {pk: row for pk, row in rows.items() if pk in baseline[kind]}
            for kind, rows in snap.items()}


def build_world():
    """Accounts in every role, a break-glass account with MFA and recovery
    codes, and accounts sitting exactly where each seed would create one."""
    from django_otp.plugins.otp_static.models import StaticDevice, StaticToken
    from django_otp.plugins.otp_totp.models import TOTPDevice

    nodes = {n.code: n for n in GeoNode.objects.all()}
    hyd = nodes['IN-TG-HYD']
    made = []

    def operator(email, phone, **extra):
        user = User.objects.create_user(
            phone_number=phone, username=phone, email=email, full_name=email, role='admin',
            is_staff=True, password='long-long-long-1', **extra)
        made.append(user)
        return user

    for index, template in enumerate(ROLE_TEMPLATES):
        role = AdminRole.objects.get(code=template['code'])
        user = operator(f'existing.{template["code"]}@saaradhigo.test', f'+91966600{index:04d}')
        scope = nodes['GLOBAL'] if role.is_global_only else hyd
        AdminAssignment.objects.create(admin=user, role=role, scope=scope)

    # The break-glass account: MFA enrolled, recovery codes issued.
    glass = operator('existing.glass@saaradhigo.test', '+919666009999', is_break_glass=True,
                     is_superuser=True)
    TOTPDevice.objects.create(user=glass, name='operator-totp', confirmed=True)
    static = StaticDevice.objects.create(user=glass, name='operator-recovery', confirmed=True)
    for token in ('abcd1234', 'efgh5678'):
        StaticToken.objects.create(device=static, token=token)

    # Collisions: an account where each seed would create one, with a USABLE
    # password and authority the seed must not touch or "repair".
    from servers.admin_access.management.commands.seed_admin_access import TEST_ADMINS

    safety_role = AdminRole.objects.get(code='safety_officer')
    for index, (email, role_code, scope_code) in enumerate(TEST_ADMINS):
        victim = operator(email, f'+91880000{index:04d}', is_superuser=(index == 0),
                          ops_role='finance')
        if role_code == 'safety_officer':
            # A deactivated assignment must stay deactivated.
            AdminAssignment.objects.create(admin=victim, role=safety_role,
                                           scope=nodes[scope_code], is_active=False)
        elif role_code == 'support_agent':
            # An existing assignment (a different reason) must stay as it is.
            AdminAssignment.objects.create(
                admin=victim, role=AdminRole.objects.get(code=role_code),
                scope=nodes[scope_code], reason='granted by a person')
    for phone, role in (('+919777000001', 'admin'), ('+919777000002', 'rider'),
                        ('+919777000003', 'driver')):
        User.objects.create_user(phone_number=phone, username=phone, role=role,
                                 is_staff=(role == 'admin'), password='someone-elses-1')
    from servers.admin_access.management.commands import seed_us_test_city as us

    for spec in [*us.DRIVERS, us.RIDER]:
        User.objects.create_user(phone_number=spec['phone'], username=spec['phone'],
                                 role='driver', password='someone-elses-2')
    User.objects.create_user(phone_number='+9190000' + '00000', username='+919000000000',
                             role='rider', password='someone-elses-3')
    User.objects.create_user(phone_number='+9195000' + '00000', username='+919500000000',
                             role='driver', password='someone-elses-4')
    return made[0]   # a Super Admin: the actor


def run(name, actor):
    kwargs = dict(REGISTRY[name])
    if accepts_actor(name):
        kwargs['actor_email'] = actor.email
    elif name in ('bootstrap_qa_accounts', 'bootstrap_qa_operator'):
        kwargs['actor_email'] = actor.email
    call_command(name, stdout=StringIO(), stderr=StringIO(), **kwargs)


def test_running_every_seed_twice_changes_no_account(seed_env):
    from conftest import seed_admin_access

    # The geo tree and the role templates (configuration, not accounts).
    seed_admin_access()
    actor = build_world()
    baseline = snapshot()

    for name in ORDER:
        run(name, actor)
        after_first = snapshot()
        assert restricted(after_first, baseline) == baseline, (
            f'{name} changed an account that already existed')

        run(name, actor)
        assert snapshot() == after_first, f'{name} is not idempotent on accounts'

    # And the new accounts the seeds did create have no usable password
    # (the test admins), while the existing ones kept theirs.
    for email in ('qa.cityops@saaradhigo.test',):
        assert User.objects.get(email=email).has_usable_password()
    assert User.objects.get(email='existing.super_admin@saaradhigo.test').has_usable_password()


def test_new_test_admins_are_created_with_no_usable_password(seed_env):
    from conftest import seed_actor, seed_admin_access

    actor = seed_actor()
    call_command('seed_admin_access', test_admins=True, actor_email=actor.email, stdout=StringIO())

    created = User.objects.filter(email__endswith='@saaradhigo.test', email__startswith='qa.')
    assert created.count() >= 8
    assert not any(u.has_usable_password() for u in created)
    assert seed_admin_access  # imported for the shared helper's side effects


def test_a_seed_still_updates_role_templates():
    """Templates are configuration, not accounts: the seed keeps them current."""
    from conftest import seed_admin_access

    seed_admin_access()
    role = AdminRole.objects.get(code='city_ops_manager')
    role.permissions = ['trip.read']
    role.save()

    seed_admin_access()

    assert 'safety.read' in AdminRole.objects.get(code='city_ops_manager').permissions


def test_the_seed_module_docstring_says_templates_are_not_accounts():
    spec = importlib.util.spec_from_file_location(
        'seed_admin_access_doc', seed_commands()['seed_admin_access'])
    text = seed_commands()['seed_admin_access'].read_text(encoding='utf-8')
    assert spec is not None
    assert 'Role templates' in text and 'NOT accounts' in text
