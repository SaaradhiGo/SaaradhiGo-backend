"""No write to an access row happens without an audit row. From anywhere.

THE FAILURE THIS CLOSES
-----------------------
On 2026-10-27 `migrate_operators --plan qa --apply` granted Super Admin at
Global, City Ops Manager at Hyderabad and the break-glass flag on QA and
wrote nothing to `AdminAuditLog`. Audit rows were written by views that
chose to; a management command is not a view. The log recorded the front
door and not the side door.

THE RULING (owner, 2026-10-27)
------------------------------
Enforce at the model layer: every create, update and delete of an
assignment, a role, a scope or the break-glass flag appends a row, whether
it comes from a view, a command, a seed or a shell -- and a test fails if
any such write happens without one.

HOW THESE TESTS ARE SHAPED
--------------------------
They write through the ORM directly, with no helper, because that is the
path a shell or a script uses. Each asserts the row count grows by exactly
one and that the row says what changed. Unchanged saves must write nothing:
a log that fills with "nothing happened" is a log people stop reading.

The last class enumerates the models of `servers.admin_access` from the
app registry and fails if one is missing from the audited list, so a new
model cannot be added to that app without a decision about auditing it.
"""

from io import StringIO

import pytest
from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db.models.signals import post_delete, post_save, pre_save
from django.test import RequestFactory

from servers.admin_access.management.commands.migrate_operators import QA_PLAN
from servers.admin_access.management.commands.seed_admin_access import GEO
from servers.admin_access.models import (
    ACTIVE,
    CITY,
    COUNTRY,
    GLOBAL,
    PAUSED,
    STATE,
    AdminAssignment,
    AdminRole,
    GeoNode,
)
from servers.admin_access.roles import ROLE_TEMPLATES
from servers.admin_audit.access_audit import (
    ACCESS_MODELS,
    AccessAuditedQuerySet,
    spec_for,
)
from servers.admin_audit.context import audit_context, current_context
from servers.admin_audit.hashchain import verify_chain
from servers.admin_audit.middleware import AuditActorMiddleware
from servers.admin_audit.models import AdminAuditLog
from conftest import seed_actor

pytestmark = pytest.mark.django_db

User = get_user_model()


def _count():
    return AdminAuditLog.objects.count()


def _latest():
    return AdminAuditLog.objects.order_by('-id').first()


def _rows_since(count):
    return list(AdminAuditLog.objects.order_by('id')[count:])


def _operator(phone='+919700000001', email='op@saaradhigo.test', **extra):
    extra.setdefault('role', 'admin')
    extra.setdefault('is_staff', True)
    return User.objects.create_user(phone_number=phone, username=phone,
                                    email=email, **extra)


def _rider(phone='+919700000099', email='rider@saaradhigo.test'):
    return User.objects.create_user(phone_number=phone, username=phone,
                                    email=email, role='rider')


@pytest.fixture
def tree():
    """GLOBAL > IN > IN-TG > IN-TG-HYD, built directly."""
    nodes = {}
    spec = [(GLOBAL, 'GLOBAL', 'Global', None), (COUNTRY, 'IN', 'India', 'GLOBAL'),
            (STATE, 'IN-TG', 'Telangana', 'IN'), (CITY, 'IN-TG-HYD', 'Hyderabad', 'IN-TG')]
    for kind, code, name, parent in spec:
        node = GeoNode(kind=kind, code=code, name=name,
                       parent=nodes.get(parent), status=ACTIVE)
        node.save()
        nodes[code] = node
    return nodes


@pytest.fixture
def role():
    return AdminRole.objects.create(code='city_ops', name='City Ops',
                                    permissions=['trip.read'])


@pytest.fixture
def operator():
    return _operator()


@pytest.fixture
def assignment(tree, role, operator):
    return AdminAssignment.objects.create(admin=operator, role=role,
                                          scope=tree['IN-TG-HYD'])


# ---------------------------------------------------------------------------
# AdminAssignment: the authority itself
# ---------------------------------------------------------------------------

class TestAssignmentWrites:
    def test_create_writes_one_row(self, tree, role, operator):
        before = _count()

        made = AdminAssignment.objects.create(admin=operator, role=role,
                                              scope=tree['IN-TG-HYD'])

        assert _count() == before + 1
        row = _latest()
        assert row.action == 'access_assignment_created'
        assert row.target_type == 'AdminAssignment'
        assert row.target_id == str(made.pk)
        assert row.before == {}
        assert row.after['admin_id'] == operator.pk
        assert row.after['role'] == 'city_ops'
        assert row.after['scope'] == 'IN-TG-HYD'
        assert row.after['is_active'] is True

    def test_update_writes_one_row_with_the_diff(self, assignment):
        before = _count()

        assignment.is_active = False
        assignment.save()

        assert _count() == before + 1
        row = _latest()
        assert row.action == 'access_assignment_updated'
        assert row.target_id == str(assignment.pk)
        assert row.before['is_active'] is True
        assert row.after['is_active'] is False
        assert row.after['changed_fields'] == ['is_active']

    def test_update_fields_save_is_audited(self, assignment):
        before = _count()

        assignment.is_active = False
        assignment.save(update_fields=['is_active'])

        assert _count() == before + 1
        assert _latest().after['is_active'] is False

    def test_an_unchanged_save_writes_nothing(self, assignment):
        before = _count()

        assignment.save()
        assignment.save(update_fields=['is_active'])

        assert _count() == before

    def test_delete_writes_one_row(self, assignment):
        pk = assignment.pk
        before = _count()

        assignment.delete()

        assert _count() == before + 1
        row = _latest()
        assert row.action == 'access_assignment_deleted'
        assert row.target_id == str(pk)
        assert row.before['role'] == 'city_ops'
        assert row.after == {}

    def test_queryset_update_is_audited(self, assignment):
        before = _count()

        AdminAssignment.objects.filter(pk=assignment.pk).update(is_active=False)

        assert _count() == before + 1
        row = _latest()
        assert row.action == 'access_assignment_updated'
        assert row.before['is_active'] is True and row.after['is_active'] is False

    def test_queryset_update_that_changes_nothing_writes_nothing(self, assignment):
        before = _count()

        AdminAssignment.objects.filter(pk=assignment.pk).update(is_active=True)

        assert _count() == before

    def test_queryset_delete_is_audited(self, assignment):
        before = _count()

        AdminAssignment.objects.filter(pk=assignment.pk).delete()

        assert _count() == before + 1
        assert _latest().action == 'access_assignment_deleted'

    def test_bulk_create_is_audited_per_row(self, tree, role, operator):
        other = _operator(phone='+919700000002', email='op2@saaradhigo.test')
        before = _count()

        AdminAssignment.objects.bulk_create([
            AdminAssignment(admin=operator, role=role, scope=tree['IN-TG']),
            AdminAssignment(admin=other, role=role, scope=tree['IN-TG-HYD']),
        ])

        rows = _rows_since(before)
        assert len(rows) == 2
        assert {r.action for r in rows} == {'access_assignment_created'}
        assert {r.after['admin_id'] for r in rows} == {operator.pk, other.pk}

    def test_bulk_update_is_audited_per_changed_row(self, tree, role, operator):
        first = AdminAssignment.objects.create(admin=operator, role=role,
                                               scope=tree['IN-TG'])
        second = AdminAssignment.objects.create(admin=operator, role=role,
                                                scope=tree['IN-TG-HYD'])
        before = _count()

        first.is_active = False
        AdminAssignment.objects.bulk_update([first, second], ['is_active'])

        rows = _rows_since(before)
        assert [r.target_id for r in rows] == [str(first.pk)]
        assert rows[0].action == 'access_assignment_updated'

    def test_deleting_the_user_is_refused_before_anything_cascades(
        self, assignment, operator,
    ):
        """Staff are deactivated, never deleted (owner ruling, 2026-10-27;
        `servers.auth_user.erasure`). The refusal aborts the collector, so
        the assignment is not deleted either and no row is written."""
        from servers.auth_user.erasure import AccountDeletionRefused

        before = _count()

        with pytest.raises(AccountDeletionRefused):
            operator.delete()

        assert AdminAssignment.objects.filter(pk=assignment.pk).exists()
        assert _count() == before


# ---------------------------------------------------------------------------
# AdminRole: editing one edits everybody holding it
# ---------------------------------------------------------------------------

class TestRoleWrites:
    def test_create(self):
        before = _count()

        made = AdminRole.objects.create(code='finance', name='Finance',
                                        permissions=['finance.read'])

        assert _count() == before + 1
        row = _latest()
        assert (row.action, row.target_type, row.target_id) == \
            ('access_role_created', 'AdminRole', str(made.pk))
        assert row.after['permissions'] == ['finance.read']

    def test_permission_change(self, role):
        before = _count()

        role.permissions = ['trip.read', 'finance.payout']
        role.save()

        assert _count() == before + 1
        row = _latest()
        assert row.action == 'access_role_updated'
        assert row.before['permissions'] == ['trip.read']
        assert row.after['permissions'] == ['finance.payout', 'trip.read']
        assert row.after['changed_fields'] == ['permissions']

    def test_unchanged_update_or_create_writes_nothing(self, role):
        before = _count()

        AdminRole.objects.update_or_create(
            code='city_ops',
            defaults={'name': 'City Ops', 'permissions': ['trip.read']},
        )

        assert _count() == before

    def test_delete(self, role):
        pk = role.pk
        before = _count()

        role.delete()

        assert _count() == before + 1
        row = _latest()
        assert (row.action, row.target_id) == ('access_role_deleted', str(pk))
        assert row.before['code'] == 'city_ops'

    def test_queryset_update(self, role):
        before = _count()

        AdminRole.objects.filter(pk=role.pk).update(is_active=False)

        assert _count() == before + 1
        assert _latest().after['is_active'] is False


# ---------------------------------------------------------------------------
# GeoNode: what a scope covers
# ---------------------------------------------------------------------------

class TestScopeWrites:
    def test_create(self):
        before = _count()

        node = GeoNode(kind=GLOBAL, code='GLOBAL', name='Global')
        node.save()

        assert _count() == before + 1
        row = _latest()
        assert (row.action, row.target_type, row.target_id) == \
            ('access_scope_created', 'GeoNode', str(node.pk))
        assert row.after['path'] == 'GLOBAL/'

    def test_status_change(self, tree):
        city = tree['IN-TG-HYD']
        before = _count()

        city.status = PAUSED
        city.save()

        assert _count() == before + 1
        row = _latest()
        assert row.action == 'access_scope_updated'
        assert row.before['status'] == ACTIVE and row.after['status'] == PAUSED

    def test_unchanged_save_writes_nothing_even_through_children(self, tree):
        before = _count()

        tree['GLOBAL'].save()   # re-saves the whole subtree

        assert _count() == before

    def test_delete(self, tree):
        city = tree['IN-TG-HYD']
        pk = city.pk
        before = _count()

        city.delete()

        assert _count() == before + 1
        row = _latest()
        assert (row.action, row.target_id) == ('access_scope_deleted', str(pk))
        assert row.before['code'] == 'IN-TG-HYD'


# ---------------------------------------------------------------------------
# The user's authority flags, break-glass above all
# ---------------------------------------------------------------------------

class TestUserAuthorityWrites:
    def test_creating_an_operator_writes_one_row(self):
        before = _count()

        made = _operator()

        assert _count() == before + 1
        row = _latest()
        assert (row.action, row.target_type, row.target_id) == \
            ('operator_account_created', 'User', str(made.pk))
        assert row.after['is_staff'] is True
        assert row.after['is_break_glass'] is False

    def test_creating_a_rider_writes_nothing(self):
        before = _count()

        _rider()

        assert _count() == before

    def test_setting_break_glass_writes_one_row(self, operator):
        before = _count()

        operator.is_break_glass = True
        operator.save(update_fields=['is_break_glass'])

        assert _count() == before + 1
        row = _latest()
        assert row.action == 'operator_authority_changed'
        assert row.target_id == str(operator.pk)
        assert row.before['is_break_glass'] is False
        assert row.after['is_break_glass'] is True
        assert row.after['changed_fields'] == ['is_break_glass']

    def test_clearing_break_glass_writes_one_row(self, operator):
        operator.is_break_glass = True
        operator.save()
        before = _count()

        operator.is_break_glass = False
        operator.save()

        assert _count() == before + 1
        assert _latest().after['is_break_glass'] is False

    def test_unchanged_saves_write_nothing(self, operator):
        before = _count()

        operator.save()
        operator.save(update_fields=['is_break_glass'])
        operator.full_name = 'Renamed'
        operator.save()
        operator.save(update_fields=['last_login'])

        assert _count() == before

    def test_a_rider_promoted_to_staff_writes_one_row(self):
        rider = _rider()
        before = _count()

        rider.is_staff = True
        rider.role = 'admin'
        rider.save()

        assert _count() == before + 1
        row = _latest()
        assert row.before == {'is_staff': False, 'is_superuser': False,
                              'role': 'rider', 'ops_role': 'admin',
                              'is_break_glass': False}
        assert row.after['changed_fields'] == ['is_staff', 'role']

    def test_queryset_update_of_an_authority_field_is_audited(self, operator):
        before = _count()

        User.objects.filter(pk=operator.pk).update(is_superuser=True)

        assert _count() == before + 1
        assert _latest().after['is_superuser'] is True

    def test_queryset_update_of_another_field_writes_nothing(self, operator):
        before = _count()

        User.objects.filter(pk=operator.pk).update(fcm_token='token')

        assert _count() == before

    def test_deleting_an_operator_is_refused_and_writes_nothing(self, operator):
        """`operator_account_deleted` stays in the spec for the raw-SQL path
        the hash chain exists for; through the ORM it can no longer happen,
        because staff are deactivated and never deleted."""
        from servers.auth_user.erasure import AccountDeletionRefused

        before = _count()

        with pytest.raises(AccountDeletionRefused, match='deactivated, never deleted'):
            operator.delete()

        assert User.objects.filter(pk=operator.pk).exists()
        assert _count() == before

    def test_deactivating_an_operator_is_the_supported_path(self, operator):
        operator.is_active = False
        operator.save()

        assert not User.objects.get(pk=operator.pk).is_active

    def test_deleting_a_rider_writes_nothing(self):
        rider = _rider()
        before = _count()

        rider.delete()

        assert _count() == before


# ---------------------------------------------------------------------------
# Who did it
# ---------------------------------------------------------------------------

class TestAttribution:
    def test_without_a_context_the_gap_is_visible(self, tree, role, operator):
        AdminAssignment.objects.create(admin=operator, role=role,
                                       scope=tree['IN-TG-HYD'])

        row = _latest()
        assert row.actor_id is None
        assert row.actor_label == 'unattributed write via AdminAssignment'

    def test_a_context_names_the_actor_and_the_reason(self, tree, role, operator):
        boss = _operator(phone='+919700000003', email='boss@saaradhigo.test',
                         full_name='The Boss')

        with audit_context(actor=boss, reason='Quarterly access review'):
            AdminAssignment.objects.create(admin=operator, role=role,
                                           scope=tree['IN-TG-HYD'])

        row = _latest()
        assert row.actor_id == boss.pk
        assert row.actor_label == 'The Boss'
        assert row.reason == 'Quarterly access review'

    def test_a_context_can_name_the_row_but_never_suppress_it(self, tree, role, operator):
        before = _count()

        with audit_context(actor_label='some script', action='custom_grant',
                           extra_after={'ticket': 'OPS-12'}) as ctx:
            AdminAssignment.objects.create(admin=operator, role=role,
                                           scope=tree['IN-TG-HYD'])

        assert _count() == before + 1
        row = _latest()
        assert row.action == 'custom_grant'
        assert row.after['ticket'] == 'OPS-12'
        assert row.after['role'] == 'city_ops'
        assert ctx.rows == [row]

    def test_the_context_is_cleared_afterwards(self):
        with audit_context(actor_label='x'):
            assert current_context() is not None
        assert current_context() is None

    def test_a_request_attributes_the_write_to_its_user(self, tree, role, operator):
        """What the middleware gives every view, without the view knowing."""
        request = RequestFactory().post('/anything/', REMOTE_ADDR='10.1.2.3')
        request.user = operator

        def view(req):
            AdminAssignment.objects.create(admin=operator, role=role,
                                           scope=tree['IN-TG-HYD'])
            return 'ok'

        assert AuditActorMiddleware(view)(request) == 'ok'
        row = _latest()
        assert row.actor_id == operator.pk
        assert row.ip_address == '10.1.2.3'

    def test_record_admin_action_falls_back_to_the_context_actor(self, operator):
        from servers.admin_audit.services import record_admin_action

        with audit_context(actor=operator):
            row = record_admin_action(None, action='something_else',
                                      target_type='thing', target_id=1)

        assert row.actor_id == operator.pk


# ---------------------------------------------------------------------------
# The write and its row are one transaction
# ---------------------------------------------------------------------------

class TestTheWriteAndTheRowStandOrFallTogether:
    def test_an_audit_failure_rolls_the_access_write_back(
        self, tree, role, operator, monkeypatch,
    ):
        from servers.admin_audit import access_audit

        def refuse(*args, **kwargs):
            raise RuntimeError('audit store unavailable')

        monkeypatch.setattr(access_audit, 'record_access_change', refuse)

        with pytest.raises(RuntimeError):
            AdminAssignment.objects.create(admin=operator, role=role,
                                           scope=tree['IN-TG-HYD'])

        assert not AdminAssignment.objects.exists()

    def test_a_break_glass_flip_rolls_back_too(self, operator, monkeypatch):
        from servers.admin_audit import access_audit

        def refuse(*args, **kwargs):
            raise RuntimeError('audit store unavailable')

        monkeypatch.setattr(access_audit, 'record_access_change', refuse)
        operator.is_break_glass = True

        with pytest.raises(RuntimeError):
            operator.save(update_fields=['is_break_glass'])

        operator.refresh_from_db()
        assert operator.is_break_glass is False


# ---------------------------------------------------------------------------
# The commands that write access
# ---------------------------------------------------------------------------

def _qa_accounts():
    people = {}
    for index, entry in enumerate(QA_PLAN):
        people[entry['email']] = _operator(
            phone=f'+91970000010{index}', email=entry['email'],
            full_name=entry['email'].split('@')[0],
        )
    return people


class TestMigrateOperators:
    def test_apply_writes_one_row_per_plan_line(self):
        call_command('seed_admin_access', actor_email=seed_actor().email, stdout=StringIO())
        people = _qa_accounts()
        srini = people['srini@saaradhigo.com']
        before = _count()

        call_command('migrate_operators', plan='qa', apply=True,
                     actor_email='srini@saaradhigo.com', stdout=StringIO())

        rows = _rows_since(before)
        assert len(rows) == len(QA_PLAN) == 3
        assert sorted(r.action for r in rows) == [
            'access_assignment_created', 'access_assignment_created',
            'operator_authority_changed',
        ]
        assert all(r.actor_id == srini.pk for r in rows)
        assert all("plan 'qa'" in r.reason for r in rows)
        flag = next(r for r in rows if r.action == 'operator_authority_changed')
        assert flag.target_id == str(people['admin@saaradhigo.com'].pk)
        assert flag.after['is_break_glass'] is True
        grants = {(r.after['role'], r.after['scope'])
                  for r in rows if r.action == 'access_assignment_created'}
        assert grants == {('super_admin', 'GLOBAL'), ('city_ops_manager', 'IN-TG-HYD')}

    def test_a_second_apply_writes_nothing(self):
        call_command('seed_admin_access', actor_email=seed_actor().email, stdout=StringIO())
        _qa_accounts()
        call_command('migrate_operators', plan='qa', apply=True,
                     actor_email='srini@saaradhigo.com', stdout=StringIO())
        before = _count()

        call_command('migrate_operators', plan='qa', apply=True,
                     actor_email='srini@saaradhigo.com', stdout=StringIO())

        assert _count() == before

    def test_a_dry_run_writes_nothing(self):
        call_command('seed_admin_access', actor_email=seed_actor().email, stdout=StringIO())
        _qa_accounts()
        before = _count()

        call_command('migrate_operators', plan='qa', stdout=StringIO())

        assert _count() == before

    def test_without_an_actor_an_apply_is_refused_before_any_write(self):
        """Owner ruling, 2026-10-28: --actor-email is mandatory for --apply.

        The earlier behaviour -- rows labelled with the command's name --
        was honest but left exactly the kind of row an access review has
        to chase. Now there is no such row, because there is no such write.
        """
        call_command('seed_admin_access', actor_email=seed_actor().email, stdout=StringIO())
        _qa_accounts()
        before = _count()

        with pytest.raises(CommandError, match='--actor-email'):
            call_command('migrate_operators', plan='qa', apply=True, stdout=StringIO())

        assert _count() == before
        assert not AdminAssignment.objects.exists()
        assert not User.objects.filter(is_break_glass=True).exists()

    def test_an_unknown_actor_is_refused_before_anything_is_written(self):
        call_command('seed_admin_access', actor_email=seed_actor().email, stdout=StringIO())
        _qa_accounts()
        before = _count()

        with pytest.raises(CommandError, match='matches no account'):
            call_command('migrate_operators', plan='qa', apply=True,
                         actor_email='nobody@saaradhigo.test', stdout=StringIO())

        assert _count() == before
        assert not AdminAssignment.objects.exists()


class TestSeedAdminAccess:
    def test_the_roles_and_scopes_it_creates_are_audited(self):
        seed_actor()
        before = _count()

        call_command('seed_admin_access', actor_email=seed_actor().email, stdout=StringIO())

        rows = _rows_since(before)
        roles = [r for r in rows if r.action == 'access_role_created']
        scopes = [r for r in rows if r.action == 'access_scope_created']
        assert len(roles) == len(ROLE_TEMPLATES)
        assert len(scopes) == len(GEO)
        assert {r.after['code'] for r in roles} == {t['code'] for t in ROLE_TEMPLATES}
        assert all(r.actor_label == f'{seed_actor().email} via seed_admin_access'
                   for r in rows)
        assert all(r.actor_id == seed_actor().pk for r in rows)

    def test_a_repeat_seed_writes_nothing(self):
        call_command('seed_admin_access', actor_email=seed_actor().email, stdout=StringIO())
        before = _count()

        call_command('seed_admin_access', actor_email=seed_actor().email, stdout=StringIO())

        assert _count() == before

    def test_a_role_edit_is_recorded_when_the_seed_restores_it(self):
        call_command('seed_admin_access', actor_email=seed_actor().email, stdout=StringIO())
        AdminRole.objects.filter(code='analyst').update(permissions=['pii.view'])
        before = _count()

        call_command('seed_admin_access', actor_email=seed_actor().email, stdout=StringIO())

        rows = _rows_since(before)
        assert [r.action for r in rows] == ['access_role_updated']
        assert rows[0].before['permissions'] == ['pii.view']


class TestCreateOperatorWritesOneRowNotTwo:
    def test_the_structural_row_is_the_operator_created_row(self):
        call_command('create_operator', email='new@saaradhigo.test',
                     phone='+919700000555', name='New Person',
                     actor_email=seed_actor().email, stdout=StringIO())

        user = User.objects.get(email='new@saaradhigo.test')
        rows = AdminAuditLog.objects.filter(target_type='User', target_id=str(user.pk))
        assert rows.count() == 1
        row = rows.get()
        assert row.action == 'operator_created'
        assert row.after['is_staff'] is True
        assert row.after['must_change_password'] is True


# ---------------------------------------------------------------------------
# A new access model cannot be added silently
# ---------------------------------------------------------------------------

class TestEveryAccessModelIsListed:
    def test_every_model_in_admin_access_is_in_the_audited_set(self):
        listed = {spec.label for spec in ACCESS_MODELS}

        for model in apps.get_app_config('admin_access').get_models():
            assert model._meta.label in listed, (
                f'{model._meta.label} lives in servers.admin_access but is not '
                f'in admin_audit.access_audit.ACCESS_MODELS. Every row in that '
                f'app defines who may do what; add a spec for it (or move it).'
            )

    def test_the_break_glass_flag_is_covered(self):
        spec = spec_for(User)
        assert spec is not None
        assert {'is_break_glass', 'is_staff', 'is_superuser', 'role'} <= spec.tracked

    def test_every_listed_model_has_its_receivers_connected(self):
        for spec in ACCESS_MODELS:
            model = apps.get_model(spec.label)
            for signal in (pre_save, post_save, post_delete):
                assert signal.has_listeners(model), (spec.label, signal)

    def test_every_listed_model_uses_the_audited_queryset(self):
        for spec in ACCESS_MODELS:
            model = apps.get_model(spec.label)
            assert isinstance(model._default_manager.all(), AccessAuditedQuerySet), \
                spec.label

    def test_the_sweep_below_covers_every_listed_model(self):
        assert set(_SWEEP) == {spec.label for spec in ACCESS_MODELS}


# One create / update / delete per listed model, driven from the registry,
# so the registry and the proof stay the same length.

def _sweep_geo():
    node = GeoNode(kind=GLOBAL, code='GLOBAL', name='Global')
    node.save()

    def mutate():
        node.name = 'Everywhere'
        node.save()
    return node, mutate


def _sweep_role():
    made = AdminRole.objects.create(code='r', name='R', permissions=[])

    def mutate():
        made.permissions = ['trip.read']
        made.save()
    return made, mutate


def _sweep_assignment():
    node = GeoNode(kind=GLOBAL, code='GLOBAL', name='Global')
    node.save()
    # Only `super_admin` may be held at Global (`models.check_role_at_scope`).
    made_role = AdminRole.objects.create(code='super_admin', name='SA', permissions=[])
    made = AdminAssignment.objects.create(admin=_operator(), role=made_role, scope=node)

    def mutate():
        made.is_active = False
        made.save()
    return made, mutate


def _sweep_user():
    made = _operator()

    def mutate():
        made.is_break_glass = True
        made.save()
    return made, mutate


_SWEEP = {
    'admin_access.GeoNode': _sweep_geo,
    'admin_access.AdminRole': _sweep_role,
    'admin_access.AdminAssignment': _sweep_assignment,
    'auth_user.customUser': _sweep_user,
}


@pytest.mark.parametrize('label', sorted(_SWEEP))
def test_create_update_and_delete_each_write_exactly_one_row(label):
    spec = next(s for s in ACCESS_MODELS if s.label == label)
    count = _count()

    instance, mutate = _SWEEP[label]()
    created = _rows_since(count)
    mine = [r for r in created if r.target_type == spec.target_type
            and r.target_id == str(instance.pk)]
    assert [r.action for r in mine] == [spec.created_action]
    count = _count()

    mutate()
    assert [r.action for r in _rows_since(count)] == [spec.updated_action]
    count = _count()

    if label == 'auth_user.customUser':
        # Staff are deactivated, never deleted (owner ruling, 2026-10-27):
        # the delete is refused before anything happens, so there is no
        # `deleted_action` row to find. `operator_account_deleted` stays
        # in the spec for the raw-SQL path the hash chain exists for.
        from servers.auth_user.erasure import AccountDeletionRefused

        with pytest.raises(AccountDeletionRefused):
            instance.delete()
        assert _rows_since(count) == []
    else:
        instance.delete()
        deleted = [r for r in _rows_since(count) if r.target_type == spec.target_type]
        assert [r.action for r in deleted] == [spec.deleted_action]

    assert verify_chain(AdminAuditLog.objects.all())[1] == []
