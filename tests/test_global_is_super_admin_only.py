"""Only `super_admin` may be assigned at Global (owner's ruling, 2026-10-28).

A Global assignment is unrestricted (`access.scope_paths`), so any other
role held there would be a quiet world-wide grant. The rule lives in code
(`models.check_role_at_scope`) because it joins the role's code to the
node's kind, which a CheckConstraint cannot express; so every ORM path
that writes an assignment must go through it, and that is what is proved
here: `save()`, `update()`, `bulk_create()`, and the `migrate_operators`
plan before `--apply`.
"""

import json

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from io import StringIO

from conftest import seed_actor, seed_admin_access
from servers.admin_access.management.commands.migrate_operators import plan_violations
from servers.admin_access.models import (
    GLOBAL_IS_SUPER_ADMIN_ONLY, AdminAssignment, AdminRole, GeoNode, check_role_at_scope,
)
from servers.admin_access.roles import ROLE_TEMPLATES, SUPER_ADMIN

pytestmark = pytest.mark.django_db

User = get_user_model()
OTHER_ROLES = sorted(t['code'] for t in ROLE_TEMPLATES if t['code'] != SUPER_ADMIN)

_n = iter(range(1, 9999))


@pytest.fixture
def geo(db):
    seed_admin_access()
    return {n.code: n for n in GeoNode.objects.all()}


def _operator():
    i = next(_n)
    phone = f'+9198600{i:05d}'
    return User.objects.create_user(
        phone_number=phone, username=phone, email=f'glob{i}@saaradhigo.test',
        role='admin', is_staff=True,
    )


def _codes(exc):
    """Every ValidationError code, however Django nested it."""
    err = exc.value if hasattr(exc, 'value') else exc
    if hasattr(err, 'error_dict'):
        return {e.code for errors in err.error_dict.values() for e in errors}
    return {e.code for e in err.error_list}


# ---------------------------------------------------------------------------
# The model
# ---------------------------------------------------------------------------

class TestTheModel:
    def test_super_admin_at_global_is_fine(self, geo):
        row = AdminAssignment.objects.create(
            admin=_operator(), role=AdminRole.objects.get(code=SUPER_ADMIN), scope=geo['GLOBAL'],
        )
        assert row.pk and row.scope.kind == 'global'

    @pytest.mark.parametrize('role_code', OTHER_ROLES)
    def test_every_other_template_is_refused_at_global(self, geo, role_code):
        with pytest.raises(ValidationError) as exc:
            AdminAssignment.objects.create(
                admin=_operator(), role=AdminRole.objects.get(code=role_code), scope=geo['GLOBAL'],
            )

        assert GLOBAL_IS_SUPER_ADMIN_ONLY in _codes(exc)
        assert not AdminAssignment.objects.filter(role__code=role_code, scope=geo['GLOBAL']).exists()

    @pytest.mark.parametrize('role_code', OTHER_ROLES)
    def test_every_other_template_is_fine_below_global(self, geo, role_code):
        for scope in ('IN', 'IN-TG', 'IN-TG-HYD'):
            AdminAssignment.objects.create(
                admin=_operator(), role=AdminRole.objects.get(code=role_code), scope=geo[scope],
            )

    def test_a_custom_role_is_refused_at_global_too(self, geo):
        """Not a template allowlist: the rule is on the CODE `super_admin`."""
        custom = AdminRole.objects.create(code='everything', name='Everything',
                                          permissions=['trip.read'])
        with pytest.raises(ValidationError) as exc:
            AdminAssignment.objects.create(admin=_operator(), role=custom, scope=geo['GLOBAL'])

        assert GLOBAL_IS_SUPER_ADMIN_ONLY in _codes(exc)

    def test_super_admin_still_cannot_be_held_locally(self, geo):
        with pytest.raises(ValidationError) as exc:
            AdminAssignment.objects.create(
                admin=_operator(), role=AdminRole.objects.get(code=SUPER_ADMIN), scope=geo['IN-TG-HYD'],
            )

        assert 'GLOBAL_ONLY_ROLE' in _codes(exc)

    def test_the_check_itself_names_both_sides(self, geo):
        with pytest.raises(ValidationError) as exc:
            check_role_at_scope(AdminRole.objects.get(code='support_agent'), geo['GLOBAL'])
        assert 'support_agent' in str(exc.value) and 'Global' in str(exc.value)


class TestTheUpdatePath:
    """`update()` skips `full_clean()`; it must not skip the rule."""

    @pytest.fixture
    def hyd_city_ops(self, geo):
        return AdminAssignment.objects.create(
            admin=_operator(), role=AdminRole.objects.get(code='city_ops_manager'),
            scope=geo['IN-TG-HYD'],
        )

    def test_moving_an_assignment_to_global_is_refused(self, geo, hyd_city_ops):
        with pytest.raises(ValidationError) as exc:
            AdminAssignment.objects.filter(pk=hyd_city_ops.pk).update(scope=geo['GLOBAL'])
        assert GLOBAL_IS_SUPER_ADMIN_ONLY in _codes(exc)

        with pytest.raises(ValidationError):
            AdminAssignment.objects.filter(pk=hyd_city_ops.pk).update(scope_id=geo['GLOBAL'].pk)

        hyd_city_ops.refresh_from_db()
        assert hyd_city_ops.scope == geo['IN-TG-HYD']

    def test_moving_an_assignment_within_the_tree_is_fine(self, geo, hyd_city_ops):
        AdminAssignment.objects.filter(pk=hyd_city_ops.pk).update(scope=geo['IN'])

        hyd_city_ops.refresh_from_db()
        assert hyd_city_ops.scope == geo['IN']

    def test_swapping_a_global_super_admins_role_is_refused(self, geo):
        row = AdminAssignment.objects.create(
            admin=_operator(), role=AdminRole.objects.get(code=SUPER_ADMIN), scope=geo['GLOBAL'],
        )
        with pytest.raises(ValidationError) as exc:
            AdminAssignment.objects.filter(pk=row.pk).update(
                role=AdminRole.objects.get(code='finance'),
            )
        assert GLOBAL_IS_SUPER_ADMIN_ONLY in _codes(exc)
        row.refresh_from_db()
        assert row.role.code == SUPER_ADMIN

    def test_an_update_that_touches_neither_role_nor_scope_is_untouched(self, geo, hyd_city_ops):
        assert AdminAssignment.objects.filter(pk=hyd_city_ops.pk).update(is_active=False) == 1

    def test_bulk_create_is_refused(self, geo):
        with pytest.raises(ValidationError):
            AdminAssignment.objects.bulk_create([
                AdminAssignment(admin=_operator(), role=AdminRole.objects.get(code='finance'),
                                scope=geo['GLOBAL']),
            ])
        assert not AdminAssignment.objects.filter(role__code='finance').exists()


# ---------------------------------------------------------------------------
# migrate_operators: refused before --apply
# ---------------------------------------------------------------------------

class TestMigrateOperatorsPlan:
    def _plan_file(self, tmp_path, lines):
        path = tmp_path / 'plan.json'
        path.write_text(json.dumps(lines), encoding='utf-8')
        return str(path)

    def test_plan_violations_names_the_line(self):
        problems = plan_violations([
            {'email': 'a@x.test', 'role': SUPER_ADMIN, 'scope': 'GLOBAL'},
            {'email': 'b@x.test', 'role': 'finance', 'scope': 'GLOBAL'},
            {'email': 'c@x.test', 'break_glass': True},
            {'email': 'd@x.test', 'role': SUPER_ADMIN, 'scope': 'IN'},
        ])

        assert len(problems) == 2
        assert 'line 2' in problems[0] and 'finance' in problems[0]
        assert 'line 4' in problems[1]

    def test_a_dry_run_of_a_bad_plan_is_refused(self, geo, tmp_path):
        user = _operator()
        plan = self._plan_file(tmp_path, [
            {'email': user.email, 'role': 'support_agent', 'scope': 'GLOBAL'},
        ])

        with pytest.raises(CommandError) as exc:
            call_command('migrate_operators', plan=plan, stdout=StringIO())

        assert 'support_agent' in str(exc.value) and 'GLOBAL' in str(exc.value)

    def test_an_apply_of_a_bad_plan_writes_nothing_at_all(self, geo, tmp_path):
        """The good line above the bad one is NOT applied: a plan is one act."""
        good, bad = _operator(), _operator()
        plan = self._plan_file(tmp_path, [
            {'email': good.email, 'role': 'city_ops_manager', 'scope': 'IN-TG-HYD'},
            {'email': bad.email, 'role': 'city_ops_manager', 'scope': 'GLOBAL'},
        ])

        with pytest.raises(CommandError):
            call_command('migrate_operators', '--apply', plan=plan,
                         actor_email=seed_actor().email, stdout=StringIO())

        assert not AdminAssignment.objects.filter(admin__in=[good, bad]).exists()

    def test_the_built_in_qa_plan_is_still_accepted(self, geo):
        assert plan_violations(
            __import__('servers.admin_access.management.commands.migrate_operators',
                       fromlist=['QA_PLAN']).QA_PLAN
        ) == []
        out = StringIO()
        call_command('migrate_operators', stdout=out)   # dry run of 'qa'
        assert 'DRY RUN' in out.getvalue()

    def test_a_good_plan_applies(self, geo, tmp_path):
        user = _operator()
        plan = self._plan_file(tmp_path, [
            {'email': user.email, 'role': SUPER_ADMIN, 'scope': 'GLOBAL'},
        ])

        call_command('migrate_operators', '--apply', plan=plan,
                     actor_email=seed_actor().email, stdout=StringIO())

        assert AdminAssignment.objects.filter(
            admin=user, role__code=SUPER_ADMIN, scope=geo['GLOBAL'], is_active=True,
        ).exists()
