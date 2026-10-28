"""`--actor-email` is mandatory for every command that writes access, money
or driver status, and it must name an active operator.

THE RULING (owner, 2026-10-28)
------------------------------
    --actor-email is mandatory for migrate_operators --apply and every other
    management command that writes access, money or driver status. It must
    resolve to an active operator; refuse otherwise.

HOW THESE TESTS ARE SHAPED
--------------------------
`WRITING_COMMANDS` is the one list. A meta-test walks every
`management/commands/*.py` in the repository and fails if the set of
modules that import the shared helper (`admin_audit.actor_option`) differs
from that list, in either direction: a new writing command cannot be added
without a decision, and a command cannot drop the helper quietly.

Each command then has a `Case` describing how to drive its write path and
how to fingerprint the tables it writes. The same four properties are
asserted for every case:

  * without the flag the write path is refused BEFORE anything is written;
  * an unknown, deactivated, non-operator or break-glass address is refused
    the same way;
  * a valid operator is the actor on every audit row the run appends,
    labelled `<email> via <command>`;
  * a dry run (where the command has one) needs no flag at all.

Then the two documented exceptions: the unattended fallback for the
commands a deploy runs, and the genesis write for the three commands that
can create the first operator in an empty database (owner, 2026-10-28:
"Allowed only while zero active operators exist. The account it creates is
audited with actor 'system:genesis', and the exemption closes the moment
one active operator exists.").
"""

import pathlib
import time
from dataclasses import dataclass, field
from io import StringIO

import pytest
from django.conf import settings as django_settings
from django.contrib.auth import get_user_model
from django.core.management import call_command, load_command_class
from django.core.management.base import CommandError
from django_otp.oath import TOTP
from django_otp.plugins.otp_static.models import StaticDevice
from django_otp.plugins.otp_totp.models import TOTPDevice

from base import ops_mfa
from conftest import SEED_ACTOR_EMAIL, seed_admin_access
from servers.admin_access.management.commands.migrate_operators import QA_PLAN
from servers.admin_access.models import AdminAssignment, AdminRole, GeoNode
from servers.admin_audit import actor_option
from servers.admin_audit.models import AdminAuditLog
from servers.driver.models import Driver, Vehicle, VehicleType

pytestmark = pytest.mark.django_db

User = get_user_model()

#: THE list. The meta-test below compares it with the command modules that
#: import `servers.admin_audit.actor_option`.
WRITING_COMMANDS = [
    'bootstrap_qa_accounts',
    'bootstrap_qa_operator',
    'create_operator',
    'enroll_operator_mfa',
    'migrate_operators',
    'qa_simulate_driver_eligibility',
    'record_retroactive_access_audit',
    'record_retroactive_linking_audit',
    'recompute_driver_trip_counts',
    'reset_operator_mfa',
    'seed_admin_access',
    'seed_approval_policies',
    'seed_us_test_city',
    'set_operator_password',
]

ACTOR_EMAIL = 'ops.lead@saaradhigo.test'
ACTOR_PHONE = '+919600000001'

REPO = pathlib.Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# People
# ---------------------------------------------------------------------------

def _user(phone, email, **extra):
    return User.objects.create_user(phone_number=phone, username=phone,
                                    email=email, **extra)


def _operator(phone, email, **extra):
    extra.setdefault('role', 'admin')
    extra.setdefault('is_staff', True)
    return _user(phone, email, **extra)


@pytest.fixture
def actor():
    """The operator at the keyboard: active, operator-shaped, not break-glass."""
    return _operator(ACTOR_PHONE, ACTOR_EMAIL, full_name='Ops Lead')


BAD_ACTORS = {
    'unknown': ('ghost@saaradhigo.test', 'matches no account'),
    'inactive': ('inactive@saaradhigo.test', 'deactivated'),
    'non-operator': ('rider@saaradhigo.test', 'not an operator'),
    'break-glass': ('admin@saaradhigo.test', 'break-glass'),
}


@pytest.fixture
def bad_actors():
    """Every account that must be refused, by name."""
    _operator('+919600000002', BAD_ACTORS['inactive'][0], is_active=False)
    _user('+919600000003', BAD_ACTORS['non-operator'][0], role='rider')
    _operator('+919600000004', BAD_ACTORS['break-glass'][0], is_break_glass=True)
    return BAD_ACTORS


# ---------------------------------------------------------------------------
# The cases
# ---------------------------------------------------------------------------

def _totp_code(device):
    totp = TOTP(device.bin_key, device.step, device.t0, device.digits, device.drift)
    totp.time = time.time()
    return totp.token()


def _qa_driver(phone='+917770001003'):
    user = User.objects.create_user(phone_number=phone, role='driver')
    driver = Driver.objects.create(user_id=user)
    vtype, _ = VehicleType.objects.get_or_create(type='sedan')
    vehicle = Vehicle.objects.create(driver_id=driver, vehicle_type_id=vtype,
                                     vehicle_number='AP16QA0001')
    driver.active_vehicle = vehicle
    driver.save(update_fields=['active_vehicle'])
    return driver


def _plan_accounts():
    for index, entry in enumerate(QA_PLAN):
        _operator(f'+91960000010{index}', entry['email'],
                  full_name=entry['email'].split('@')[0])


@dataclass
class Case:
    """How to drive one command's write path, and how to see what it wrote."""

    id: str
    command: str
    #: Builds the fixture state and returns the write-path arguments
    #: (positional args, option kwargs) WITHOUT the actor. Receives the
    #: pytest `settings` and `monkeypatch` fixtures.
    prepare: callable
    #: A hashable picture of the tables the command writes.
    fingerprint: callable
    #: Arguments for a dry run, or None when the command has none.
    dry_run: tuple = None
    #: May a deploy run it with `QA_BOOTSTRAP_ACTOR_EMAIL`?
    unattended: bool = False
    #: May it run with no actor when no operator exists at all?
    genesis: bool = False
    #: Preparation that itself needs an operator (seeding, enrolment) and
    #: therefore cannot run in the genesis scenario.
    needs_operator_to_prepare: bool = False
    #: The label on every row when `email` is the actor. The default is
    #: `<email> via <command>`; a command that records something done by
    #: nobody (a migration) labels the row for the thing that did it and
    #: keeps the operator in the actor snapshot.
    actor_label: callable = None
    extra: dict = field(default_factory=dict)

    def expected_label(self, email):
        if self.actor_label is not None:
            return self.actor_label(email)
        return f'{email} via {self.command}'


def _prepare_migrate(settings, monkeypatch):
    seed_admin_access()
    _plan_accounts()
    return ['--plan', 'qa', '--apply'], {}


def _fp_migrate():
    return (
        tuple(sorted(AdminAssignment.objects.values_list(
            'admin_id', 'role__code', 'scope__code', 'is_active'))),
        tuple(sorted(User.objects.filter(is_break_glass=True).values_list('pk', flat=True))),
    )


def _prepare_seed(settings, monkeypatch):
    return [], {}


def _fp_seed():
    return (tuple(sorted(GeoNode.objects.values_list('code', flat=True))),
            tuple(sorted(AdminRole.objects.values_list('code', flat=True))),
            User.objects.count(), AdminAssignment.objects.count())


def _prepare_seed_test_admins(settings, monkeypatch):
    settings.IS_PRODUCTION = False
    return ['--test-admins'], {}


def _prepare_create_operator(settings, monkeypatch):
    return ['--email', 'new.person@saaradhigo.test', '--phone', '+919600000555',
            '--name', 'New Person'], {}


def _fp_users():
    return tuple(sorted(User.objects.values_list('pk', 'role', 'is_staff', 'is_superuser')))


MFA_PHONE = '+919600000601'


def _prepare_enrol_start(settings, monkeypatch):
    _operator(MFA_PHONE, 'enrollee@saaradhigo.test')
    return ['--phone', MFA_PHONE], {}


def _prepare_enrol_confirm(settings, monkeypatch):
    user = _operator(MFA_PHONE, 'enrollee@saaradhigo.test')
    device, _uri = ops_mfa.enroll_totp(user)
    return ['--phone', MFA_PHONE, '--code', _totp_code(device)], {}


def _fp_mfa():
    return (tuple(sorted(TOTPDevice.objects.values_list('pk', 'confirmed'))),
            tuple(sorted(StaticDevice.objects.values_list('pk', flat=True))))


def _prepare_reset(settings, monkeypatch):
    user = _operator(MFA_PHONE, 'enrollee@saaradhigo.test')
    device, _uri = ops_mfa.enroll_totp(user)
    device.confirmed = True
    device.save(update_fields=['confirmed'])
    return ['--phone', MFA_PHONE, '--confirm',
            '--reason', 'lost handset, identity verified by voice call'], {}


def _prepare_set_password(settings, monkeypatch):
    _operator(MFA_PHONE, 'enrollee@saaradhigo.test')
    return ['--email', 'enrollee@saaradhigo.test'], {}


def _fp_passwords():
    return tuple(sorted(User.objects.values_list('pk', 'password')))


def _prepare_retro(settings, monkeypatch):
    seed_admin_access()
    _plan_accounts()
    call_command('migrate_operators', '--plan', 'qa', '--apply',
                 '--actor-email', 'srini@saaradhigo.com', stdout=StringIO())
    return ['--plan', 'qa', '--performed-at', '2026-10-27T01:14:00Z'], {}


def _fp_audit():
    return AdminAuditLog.objects.count()


def _prepare_retro_linking(settings, monkeypatch):
    return ['--performed-on', '2026-10-27'], {}


def _prepare_recompute(settings, monkeypatch):
    user = User.objects.create_user(phone_number='+917770001004', role='driver')
    # Stored 5, actually 0 completed trips: the one line the apply writes.
    Driver.objects.create(user_id=user, total_trips=5)
    return ['--apply'], {}


def _fp_trip_counts():
    return tuple(sorted(Driver.objects.values_list('pk', 'total_trips')))


def _prepare_simulate(settings, monkeypatch):
    settings.ENVIRONMENT = 'qa'
    settings.IS_PRODUCTION = False
    driver = _qa_driver()
    return ['--driver-id', str(driver.id)], {}


def _fp_eligibility():
    return tuple(sorted(Driver.objects.values_list('pk', 'qa_simulated_eligibility')))


def _prepare_us_seed(settings, monkeypatch):
    settings.IS_PRODUCTION = False
    seed_admin_access()
    return [], {}


def _prepare_us_seed_remove(settings, monkeypatch):
    settings.IS_PRODUCTION = False
    seed_admin_access()
    call_command('seed_us_test_city', '--actor-email', SEED_ACTOR_EMAIL,
                 stdout=StringIO())
    return ['--remove'], {}


def _fp_us_seed():
    from servers.pricing.models import ServiceZone
    from servers.ride.models import Trip

    return (tuple(sorted(ServiceZone.objects.values_list('code', flat=True))),
            User.objects.count(), Driver.objects.count(), Trip.objects.count())


def _prepare_seed_policies(settings, monkeypatch):
    return [], {}


def _fp_policies():
    from servers.approvals.models import ApprovalPolicy

    return tuple(sorted(ApprovalPolicy.objects.values_list(
        'country', 'action_key', 'version', 'maker_limit', 'is_active')))


QA_OPERATOR_PHONE = '+919555000001'


def _prepare_bootstrap_operator(settings, monkeypatch):
    monkeypatch.setenv('ENVIRONMENT', 'qa')
    monkeypatch.setenv('DEBUG_ENV', 'False')
    monkeypatch.setenv('QA_ADMIN_BOOTSTRAP_PHONE', QA_OPERATOR_PHONE)
    monkeypatch.setenv('QA_ADMIN_BOOTSTRAP_PASSWORD', 'qa-operator-not-a-real-secret')
    return [], {}


def _prepare_bootstrap_accounts(settings, monkeypatch):
    settings.TEST_PHONE_NUMBERS = {'+919999000001': '100001'}
    monkeypatch.setenv('ENVIRONMENT', 'qa')
    monkeypatch.setenv('DEBUG_ENV', 'True')
    for prefix, phone in (('QA_ADMIN', '+919850000001'), ('QA_RIDER', '+919850000002'),
                          ('QA_DRIVER', '+919850000003')):
        monkeypatch.setenv(f'{prefix}_BOOTSTRAP_PHONE', phone)
        monkeypatch.setenv(f'{prefix}_BOOTSTRAP_EMAIL', f'{prefix.lower()}@saaradhigo.test')
        monkeypatch.setenv(f'{prefix}_BOOTSTRAP_PASSWORD', 'Peacock-Junction-2026!')
    return [], {}


CASES = [
    Case('migrate_operators --apply', 'migrate_operators',
         _prepare_migrate, _fp_migrate, dry_run=(['--plan', 'qa'], {}),
         needs_operator_to_prepare=True),
    Case('seed_admin_access', 'seed_admin_access',
         _prepare_seed, _fp_seed, unattended=True),
    Case('seed_admin_access --test-admins', 'seed_admin_access',
         _prepare_seed_test_admins, _fp_seed, unattended=True),
    Case('create_operator', 'create_operator',
         _prepare_create_operator, _fp_users, genesis=True),
    Case('enroll_operator_mfa (start)', 'enroll_operator_mfa',
         _prepare_enrol_start, _fp_mfa, needs_operator_to_prepare=True),
    Case('enroll_operator_mfa --code (confirm)', 'enroll_operator_mfa',
         _prepare_enrol_confirm, _fp_mfa, needs_operator_to_prepare=True),
    Case('reset_operator_mfa --confirm', 'reset_operator_mfa',
         _prepare_reset, _fp_mfa, dry_run=(['--phone', MFA_PHONE], {}),
         needs_operator_to_prepare=True),
    Case('set_operator_password', 'set_operator_password',
         _prepare_set_password, _fp_passwords, needs_operator_to_prepare=True),
    Case('record_retroactive_access_audit', 'record_retroactive_access_audit',
         _prepare_retro, _fp_audit, needs_operator_to_prepare=True),
    Case('record_retroactive_linking_audit', 'record_retroactive_linking_audit',
         _prepare_retro_linking, _fp_audit,
         actor_label=lambda email: 'system:migration'),
    Case('recompute_driver_trip_counts --apply', 'recompute_driver_trip_counts',
         _prepare_recompute, _fp_trip_counts, dry_run=([], {})),
    Case('qa_simulate_driver_eligibility', 'qa_simulate_driver_eligibility',
         _prepare_simulate, _fp_eligibility),
    Case('seed_approval_policies', 'seed_approval_policies',
         _prepare_seed_policies, _fp_policies),
    Case('seed_us_test_city', 'seed_us_test_city',
         _prepare_us_seed, _fp_us_seed, needs_operator_to_prepare=True),
    Case('seed_us_test_city --remove', 'seed_us_test_city',
         _prepare_us_seed_remove, _fp_us_seed, needs_operator_to_prepare=True),
    Case('bootstrap_qa_operator', 'bootstrap_qa_operator',
         _prepare_bootstrap_operator, _fp_users, unattended=True, genesis=True),
    Case('bootstrap_qa_accounts', 'bootstrap_qa_accounts',
         _prepare_bootstrap_accounts, _fp_users, unattended=True, genesis=True),
]

CASE_IDS = [case.id for case in CASES]


def _run(case, args, kwargs, actor_email=None):
    args = list(args)
    if actor_email is not None:
        args += ['--actor-email', actor_email]
    out = StringIO()
    call_command(case.command, *args, stdout=out, stderr=out, **kwargs)
    return out.getvalue()


def _rows_since(count):
    return list(AdminAuditLog.objects.order_by('id')[count:])


# ---------------------------------------------------------------------------
# The list is the truth
# ---------------------------------------------------------------------------

def _command_modules():
    """Every `<app>/management/commands/<name>.py` in the repository."""
    found = {}
    for path in REPO.glob('servers/*/management/commands/*.py'):
        if path.stem.startswith('_'):
            continue
        found[path.stem] = path
    return found


class TestTheListMatchesTheCode:
    def test_the_list_is_exactly_the_modules_that_import_the_helper(self):
        modules = _command_modules()
        assert len(modules) >= len(WRITING_COMMANDS)
        importing = sorted(
            name for name, path in modules.items()
            if 'admin_audit.actor_option' in path.read_text(encoding='utf-8')
        )
        assert importing == sorted(WRITING_COMMANDS), (
            'a command imports the --actor-email helper but is not in '
            'WRITING_COMMANDS, or the other way round. Both lists must '
            'change together, with a reason.'
        )

    def test_the_list_has_no_duplicates_and_every_name_is_a_command(self):
        modules = _command_modules()
        assert len(set(WRITING_COMMANDS)) == len(WRITING_COMMANDS)
        missing = [name for name in WRITING_COMMANDS if name not in modules]
        assert missing == []

    @pytest.mark.parametrize('name', WRITING_COMMANDS)
    def test_every_listed_command_takes_the_flag(self, name):
        path = _command_modules()[name]
        app = path.parts[-4]
        command = load_command_class(f'servers.{app}', name)
        parser = command.create_parser('manage.py', name)
        flags = {flag for action in parser._actions for flag in action.option_strings}
        assert '--actor-email' in flags

    def test_every_case_names_a_listed_command(self):
        assert {case.command for case in CASES} == set(WRITING_COMMANDS)

    def test_read_only_commands_do_not_take_the_flag(self):
        """The flag means 'this writes'. A reporting command must not grow it."""
        for name in ('list_admin_access', 'check_geo_links', 'verify_audit_chain',
                     'classify_historical_settlements', 'fare_shadow_report',
                     'email_safety_check'):
            path = _command_modules()[name]
            assert 'actor_option' not in path.read_text(encoding='utf-8'), name


# ---------------------------------------------------------------------------
# The four properties, for every case
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('case', CASES, ids=CASE_IDS)
class TestEveryWritingCommand:
    def test_the_write_path_is_refused_without_an_actor(self, case, actor, settings, monkeypatch):
        args, kwargs = case.prepare(settings, monkeypatch)
        before = case.fingerprint()
        rows = AdminAuditLog.objects.count()

        with pytest.raises(CommandError, match='--actor-email'):
            _run(case, args, kwargs)

        assert case.fingerprint() == before, 'something was written before the refusal'
        assert AdminAuditLog.objects.count() == rows

    @pytest.mark.parametrize('kind', sorted(BAD_ACTORS))
    def test_an_address_that_is_not_an_active_operator_is_refused(
            self, case, kind, actor, bad_actors, settings, monkeypatch):
        email, why = bad_actors[kind]
        args, kwargs = case.prepare(settings, monkeypatch)
        before = case.fingerprint()
        rows = AdminAuditLog.objects.count()

        with pytest.raises(CommandError, match=why):
            _run(case, args, kwargs, actor_email=email)

        assert case.fingerprint() == before
        assert AdminAuditLog.objects.count() == rows

    def test_a_valid_operator_is_the_actor_on_every_row(self, case, actor, settings, monkeypatch):
        args, kwargs = case.prepare(settings, monkeypatch)
        before = case.fingerprint()
        count = AdminAuditLog.objects.count()

        _run(case, args, kwargs, actor_email=ACTOR_EMAIL)

        assert case.fingerprint() != before, 'the write path did not write'
        for row in _rows_since(count):
            assert row.actor_id == actor.pk, row.action
            assert row.actor_email == ACTOR_EMAIL, row.action
            assert row.actor_label == case.expected_label(ACTOR_EMAIL), row.action

    def test_the_address_is_matched_case_insensitively(self, case, actor, settings, monkeypatch):
        args, kwargs = case.prepare(settings, monkeypatch)
        count = AdminAuditLog.objects.count()

        _run(case, args, kwargs, actor_email=ACTOR_EMAIL.upper())

        for row in _rows_since(count):
            assert row.actor_id == actor.pk

    def test_a_dry_run_needs_no_actor(self, case, actor, settings, monkeypatch):
        if case.dry_run is None:
            pytest.skip('no dry run on this command')
        case.prepare(settings, monkeypatch)
        before = case.fingerprint()
        rows = AdminAuditLog.objects.count()

        args, kwargs = case.dry_run
        _run(case, args, kwargs)   # no exception

        assert case.fingerprint() == before
        assert AdminAuditLog.objects.count() == rows


# ---------------------------------------------------------------------------
# The refusal says why
# ---------------------------------------------------------------------------

class TestTheMessages:
    def test_the_break_glass_refusal_explains_itself(self, bad_actors):
        with pytest.raises(CommandError) as exc:
            actor_option.resolve_actor(bad_actors['break-glass'][0])
        text = str(exc.value)
        assert 'break-glass' in text
        assert 'emergency' in text
        assert 'names nobody' in text

    def test_a_superuser_counts_as_an_operator(self):
        root = _user('+919600000777', 'root@saaradhigo.test', role='rider',
                     is_superuser=True)
        assert actor_option.resolve_actor('root@saaradhigo.test') == root

    def test_the_missing_flag_message_names_the_command(self, actor):
        with pytest.raises(CommandError) as exc:
            actor_option.require_actor({}, 'some_command')
        assert 'some_command' in str(exc.value)
        assert '--actor-email' in str(exc.value)
        assert 'Nothing was changed' in str(exc.value)


# ---------------------------------------------------------------------------
# Unattended runs: the deploy names the actor through a setting
# ---------------------------------------------------------------------------

UNATTENDED = [case for case in CASES if case.unattended]
ATTENDED_ONLY = [case for case in CASES if not case.unattended]


@pytest.mark.parametrize('case', UNATTENDED, ids=[c.id for c in UNATTENDED])
class TestUnattendedRuns:
    def test_the_setting_stands_in_for_the_flag(self, case, actor, settings, monkeypatch):
        args, kwargs = case.prepare(settings, monkeypatch)
        settings.QA_BOOTSTRAP_ACTOR_EMAIL = ACTOR_EMAIL
        count = AdminAuditLog.objects.count()

        _run(case, args, kwargs)

        rows = _rows_since(count)
        for row in rows:
            assert row.actor_id == actor.pk
            assert row.actor_label == case.expected_label(ACTOR_EMAIL)

    @pytest.mark.parametrize('kind', sorted(BAD_ACTORS))
    def test_the_setting_is_validated_like_the_flag(
            self, case, kind, actor, bad_actors, settings, monkeypatch):
        email, why = bad_actors[kind]
        args, kwargs = case.prepare(settings, monkeypatch)
        settings.QA_BOOTSTRAP_ACTOR_EMAIL = email
        before = case.fingerprint()

        with pytest.raises(CommandError, match=why) as exc:
            _run(case, args, kwargs)

        assert 'QA_BOOTSTRAP_ACTOR_EMAIL' in str(exc.value)
        assert case.fingerprint() == before

    def test_the_flag_wins_over_the_setting(self, case, actor, bad_actors, settings, monkeypatch):
        args, kwargs = case.prepare(settings, monkeypatch)
        settings.QA_BOOTSTRAP_ACTOR_EMAIL = bad_actors['break-glass'][0]
        count = AdminAuditLog.objects.count()

        _run(case, args, kwargs, actor_email=ACTOR_EMAIL)

        for row in _rows_since(count):
            assert row.actor_id == actor.pk

    def test_the_missing_flag_message_mentions_the_setting(self, case, actor, settings, monkeypatch):
        args, kwargs = case.prepare(settings, monkeypatch)
        settings.QA_BOOTSTRAP_ACTOR_EMAIL = ''

        with pytest.raises(CommandError) as exc:
            _run(case, args, kwargs)

        assert 'QA_BOOTSTRAP_ACTOR_EMAIL' in str(exc.value)


@pytest.mark.parametrize('case', ATTENDED_ONLY, ids=[c.id for c in ATTENDED_ONLY])
def test_a_command_a_person_runs_ignores_the_setting(case, actor, settings, monkeypatch):
    """The deploy fallback is for the deploy path only."""
    args, kwargs = case.prepare(settings, monkeypatch)
    settings.QA_BOOTSTRAP_ACTOR_EMAIL = ACTOR_EMAIL
    before = case.fingerprint()

    with pytest.raises(CommandError, match='--actor-email'):
        _run(case, args, kwargs)

    assert case.fingerprint() == before


class TestTheSettingIsIgnoredInProduction:
    """`seed_admin_access` is the one unattended command that also runs in
    production (geo and roles are configuration). There, a person passes
    the flag; the setting is not consulted."""

    def test_seed_in_production_refuses_the_setting(self, actor, settings, monkeypatch):
        settings.IS_PRODUCTION = True
        settings.QA_BOOTSTRAP_ACTOR_EMAIL = ACTOR_EMAIL

        with pytest.raises(CommandError, match='production') as exc:
            call_command('seed_admin_access', stdout=StringIO())

        assert '--actor-email' in str(exc.value)
        assert not GeoNode.objects.exists()

    def test_seed_in_production_accepts_the_flag(self, actor, settings):
        settings.IS_PRODUCTION = True
        count = AdminAuditLog.objects.count()

        call_command('seed_admin_access', '--actor-email', ACTOR_EMAIL, stdout=StringIO())

        assert GeoNode.objects.exists()
        rows = _rows_since(count)
        assert rows
        assert all(r.actor_id == actor.pk for r in rows)

    def test_the_setting_exists_and_defaults_to_empty(self):
        """It is a documented setting read from the environment, not an
        undeclared `os.environ` lookup inside a command."""
        assert hasattr(django_settings, 'QA_BOOTSTRAP_ACTOR_EMAIL')
        assert actor_option.UNATTENDED_SETTING == 'QA_BOOTSTRAP_ACTOR_EMAIL'
        source = (REPO / 'base' / 'settings.py').read_text(encoding='utf-8')
        assert "os.environ.get('QA_BOOTSTRAP_ACTOR_EMAIL')" in source


# ---------------------------------------------------------------------------
# Genesis: the first operator has nobody to be created by
# ---------------------------------------------------------------------------

GENESIS = [case for case in CASES if case.genesis]
NOT_GENESIS = [case for case in CASES if not case.genesis and not case.needs_operator_to_prepare]


@pytest.mark.parametrize('case', GENESIS, ids=[c.id for c in GENESIS])
class TestGenesis:
    def test_with_no_operator_at_all_the_first_one_can_be_made(self, case, settings, monkeypatch):
        args, kwargs = case.prepare(settings, monkeypatch)
        assert not actor_option.any_operator_exists()
        count = AdminAuditLog.objects.count()

        _run(case, args, kwargs)

        assert actor_option.any_operator_exists()
        rows = _rows_since(count)
        assert rows, 'the genesis write left no row'
        for row in rows:
            assert row.actor_id is None
            assert row.actor_email == ''
            assert row.actor_label == 'system:genesis'
            assert row.after.get('genesis') is True

    def test_a_deactivated_operator_does_not_count(self, case, settings, monkeypatch):
        """An operator nobody can sign in as is no operator for this purpose."""
        _operator('+919600000002', 'inactive@saaradhigo.test', is_active=False)
        args, kwargs = case.prepare(settings, monkeypatch)

        _run(case, args, kwargs)   # genesis still applies

        assert actor_option.any_operator_exists()

    def test_once_one_active_operator_exists_the_exemption_is_gone(
            self, case, actor, settings, monkeypatch):
        """ONE active operator -- the `actor` fixture -- closes it."""
        assert actor_option.any_operator_exists()
        args, kwargs = case.prepare(settings, monkeypatch)
        before = case.fingerprint()
        rows = AdminAuditLog.objects.count()

        with pytest.raises(CommandError, match='--actor-email'):
            _run(case, args, kwargs)

        assert case.fingerprint() == before
        assert AdminAuditLog.objects.count() == rows
        assert not AdminAuditLog.objects.filter(actor_label='system:genesis').exists()

    def test_an_active_break_glass_operator_also_closes_it(self, case, settings, monkeypatch):
        """The emergency account cannot be the ACTOR of a routine command,
        but it is an operator somebody can sign in as, so while it exists
        the database is not one with zero active operators."""
        _operator('+919600000004', 'admin@saaradhigo.test', is_break_glass=True)
        assert actor_option.any_operator_exists()
        args, kwargs = case.prepare(settings, monkeypatch)
        before = case.fingerprint()

        with pytest.raises(CommandError, match='--actor-email'):
            _run(case, args, kwargs)

        assert case.fingerprint() == before

    def test_the_same_run_with_an_actor_is_not_a_genesis_write(
            self, case, actor, settings, monkeypatch):
        args, kwargs = case.prepare(settings, monkeypatch)
        count = AdminAuditLog.objects.count()

        _run(case, args, kwargs, actor_email=ACTOR_EMAIL)

        rows = _rows_since(count)
        assert rows
        for row in rows:
            assert row.actor_id == actor.pk
            assert row.actor_label != 'system:genesis'
            assert row.after.get('genesis') is False


def test_the_genesis_label_is_exactly_system_genesis():
    """One string, by ruling, whichever command wrote the row."""
    assert actor_option.GENESIS_LABEL == 'system:genesis'
    for name in ('create_operator', 'bootstrap_qa_operator', 'bootstrap_qa_accounts'):
        assert actor_option.genesis_label(name) == 'system:genesis'
        assert actor_option.actor_label(None, name) == 'system:genesis'


@pytest.mark.parametrize('case', NOT_GENESIS, ids=[c.id for c in NOT_GENESIS])
def test_other_commands_have_no_genesis_exemption(case, settings, monkeypatch):
    """An empty database is not a licence for anything but the first operator."""
    args, kwargs = case.prepare(settings, monkeypatch)
    assert not actor_option.any_operator_exists()
    before = case.fingerprint()

    with pytest.raises(CommandError, match='--actor-email'):
        _run(case, args, kwargs)

    assert case.fingerprint() == before
