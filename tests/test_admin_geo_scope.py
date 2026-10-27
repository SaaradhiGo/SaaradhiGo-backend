"""Geo scope, assignments and editable roles.

WHAT THESE ARE PROTECTING
-------------------------
SaaradhiGo operates in India and the USA. Before this, `ops_role` was a
single global field: a Hyderabad City Ops Manager could act on every city
in every country, and nothing in any response said so.

The tests that matter most are the DENIALS, and in both directions — an
India-scoped admin must not reach US data, and a US-scoped admin must not
reach India's. A permission model is only as good as what it refuses.

Two halves are tested separately on purpose, because they fail differently:

  * `has_permission_at` — may this admin do this, here?
  * `scope_queryset`    — which rows may they see?

A view with a correct permission check and an unfiltered queryset passes
the first and hands over the world. That is the bug this file exists for.
"""

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError

from servers.admin_access.access import (
    can_see_pii,
    has_permission_at,
    is_super_admin,
    permissions_at,
    scope_queryset,
)
from servers.admin_access.models import (
    ACTIVE,
    CITY,
    COUNTRY,
    GLOBAL,
    STATE,
    AdminAssignment,
    AdminRole,
    GeoNode,
)
from servers.admin_access.roles import (
    MIN_SUPER_ADMINS, ROLE_TEMPLATES, SUPER_ADMIN, validate_templates,
)
from servers.admin_access.services import (
    WouldLeaveTooFewSuperAdmins,
    active_super_admin_ids,
    revoke_assignment,
    super_admin_shortfall,
)

pytestmark = pytest.mark.django_db

User = get_user_model()


@pytest.fixture
def geo(db):
    """A two-country tree, which is the whole point."""
    from django.core.management import call_command

    call_command('seed_admin_access')
    return {node.code: node for node in GeoNode.objects.all()}


def make_admin(email, phone):
    return User.objects.create_user(
        phone_number=phone, username=phone, email=email,
        role='admin', is_staff=True,
    )


def assign(user, role_code, scope):
    return AdminAssignment.objects.create(
        admin=user, role=AdminRole.objects.get(code=role_code), scope=scope,
    )


# ---------------------------------------------------------------------------
# The hierarchy
# ---------------------------------------------------------------------------

class TestTheHierarchy:
    def test_the_seed_builds_both_countries(self, geo):
        assert geo['IN'].kind == COUNTRY
        assert geo['US'].kind == COUNTRY
        assert geo['IN-TG'].kind == STATE
        assert geo['IN-TG-HYD'].kind == CITY

    def test_state_is_not_optional(self, geo):
        """A city must sit under a state. US rules are state-level, and a
        city hanging off a country would break every one of them."""
        with pytest.raises(ValidationError):
            GeoNode(kind=CITY, code='US-NOPE', name='Nowhere',
                    parent=geo['US']).save()

    def test_paths_are_built_from_the_tree(self, geo):
        assert geo['IN-TG-HYD'].path == 'GLOBAL/IN/IN-TG/IN-TG-HYD/'

    def test_a_node_covers_its_descendants(self, geo):
        assert geo['IN'].covers(geo['IN-TG-HYD'])
        assert geo['GLOBAL'].covers(geo['US-TX-AUS'])

    def test_a_node_does_not_cover_a_sibling(self, geo):
        assert not geo['IN'].covers(geo['US-TX-AUS'])
        assert not geo['IN-TG'].covers(geo['IN-AP-VJA'])

    def test_renaming_a_parent_rewrites_descendant_paths(self, geo):
        """Otherwise descendants keep an orphaned prefix and silently fall
        outside every scope check."""
        state = geo['IN-TG']
        state.code = 'IN-TEL'
        state.save()

        city = GeoNode.objects.get(code='IN-TG-HYD')
        assert city.path.startswith('GLOBAL/IN/IN-TEL/')

    def test_only_hyderabad_is_live(self, geo):
        live = set(GeoNode.objects.filter(kind=CITY, status=ACTIVE)
                   .values_list('code', flat=True))
        assert live == {'IN-TG-HYD'}


# ---------------------------------------------------------------------------
# Roles are data
# ---------------------------------------------------------------------------

class TestRoles:
    def test_all_eight_are_seeded(self, geo):
        assert AdminRole.objects.filter(is_template=True).count() == 8

    def test_every_seeded_permission_exists(self):
        """A permission string that is not in the catalogue grants nothing
        silently, which is the worst kind of access bug."""
        assert validate_templates() == []

    def test_the_analyst_has_no_pii(self, geo):
        analyst = AdminRole.objects.get(code='analyst')

        assert 'pii.view' not in analyst.permissions
        assert 'report.read' in analyst.permissions

    def test_super_admin_is_global_only(self, geo):
        assert AdminRole.objects.get(code=SUPER_ADMIN).is_global_only is True

    def test_a_global_only_role_cannot_be_held_locally(self, geo):
        user = make_admin('x@saaradhigo.test', '+918800000090')

        with pytest.raises(ValidationError):
            assign(user, SUPER_ADMIN, geo['IN-TG-HYD'])

    def test_roles_are_editable(self, geo):
        """Super Admin can create custom roles; the templates are a
        starting point, not a fixed class hierarchy."""
        custom = AdminRole.objects.create(
            code='night_ops', name='Night Ops',
            permissions=['trip.read', 'safety.respond'],
        )

        assert custom.grants('safety.respond')
        assert not custom.grants('finance.payout')


# ---------------------------------------------------------------------------
# What an assignment grants
# ---------------------------------------------------------------------------

class TestPermissionsAreScoped:
    @pytest.fixture
    def city_ops(self, geo):
        user = make_admin('hyd.ops@saaradhigo.test', '+918800000001')
        assign(user, 'city_ops_manager', geo['IN-TG-HYD'])
        return user

    def test_granted_at_the_assigned_city(self, city_ops, geo):
        assert has_permission_at(city_ops, 'driver.write', geo['IN-TG-HYD'])

    def test_denied_at_a_sibling_city(self, city_ops, geo):
        assert not has_permission_at(city_ops, 'driver.write', geo['IN-AP-VJA'])

    def test_denied_at_the_parent_state(self, city_ops, geo):
        """A city assignment covers downwards, never upwards."""
        assert not has_permission_at(city_ops, 'driver.write', geo['IN-TG'])

    def test_a_state_assignment_covers_its_cities(self, geo):
        user = make_admin('tg.ops@saaradhigo.test', '+918800000002')
        assign(user, 'city_ops_manager', geo['IN-TG'])

        assert has_permission_at(user, 'driver.write', geo['IN-TG-HYD'])
        assert has_permission_at(user, 'driver.write', geo['IN-TG-WGL'])
        assert not has_permission_at(user, 'driver.write', geo['IN-AP-VJA'])

    def test_a_country_assignment_covers_its_states(self, geo):
        user = make_admin('in.ops@saaradhigo.test', '+918800000003')
        assign(user, 'city_ops_manager', geo['IN'])

        assert has_permission_at(user, 'driver.write', geo['IN-AP-VJA'])
        assert has_permission_at(user, 'driver.write', geo['IN-TG-HYD'])

    def test_a_permission_the_role_lacks_is_denied_everywhere(self, city_ops, geo):
        assert not has_permission_at(city_ops, 'finance.payout', geo['IN-TG-HYD'])

    def test_several_assignments_combine(self, geo):
        user = make_admin('two.hats@saaradhigo.test', '+918800000004')
        assign(user, 'support_agent', geo['IN'])
        assign(user, 'finance', geo['IN-TG-HYD'])

        assert has_permission_at(user, 'support.reply', geo['IN-AP-VJA'])
        assert has_permission_at(user, 'finance.payout', geo['IN-TG-HYD'])
        # Finance was granted only for Hyderabad.
        assert not has_permission_at(user, 'finance.payout', geo['IN-AP-VJA'])

    def test_an_unknown_permission_is_refused_loudly(self, city_ops, geo):
        with pytest.raises(ValueError):
            has_permission_at(city_ops, 'driver.teleport', geo['IN-TG-HYD'])

    def test_an_admin_with_no_assignment_holds_nothing(self, geo):
        user = make_admin('nobody@saaradhigo.test', '+918800000005')

        assert permissions_at(user) == set()

    def test_an_inactive_assignment_grants_nothing(self, city_ops, geo):
        AdminAssignment.objects.filter(admin=city_ops).update(is_active=False)

        assert not has_permission_at(city_ops, 'driver.write', geo['IN-TG-HYD'])


# ---------------------------------------------------------------------------
# Cross-country denial — both directions
# ---------------------------------------------------------------------------

class TestCountriesAreIsolated:
    def test_an_india_admin_cannot_reach_the_us(self, geo):
        user = make_admin('india@saaradhigo.test', '+918800000010')
        assign(user, 'city_ops_manager', geo['IN'])

        assert not has_permission_at(user, 'driver.write', geo['US-TX-AUS'])
        assert not has_permission_at(user, 'driver.read', geo['US'])

    def test_a_us_admin_cannot_reach_india(self, geo):
        user = make_admin('texas@saaradhigo.test', '+918800000011')
        assign(user, 'city_ops_manager', geo['US-TX'])

        assert not has_permission_at(user, 'driver.write', geo['IN-TG-HYD'])
        assert not has_permission_at(user, 'driver.read', geo['IN'])

    def test_only_global_reaches_both(self, geo):
        user = make_admin('global@saaradhigo.test', '+918800000012')
        assign(user, SUPER_ADMIN, geo['GLOBAL'])

        assert has_permission_at(user, 'driver.write', geo['IN-TG-HYD'])
        assert has_permission_at(user, 'driver.write', geo['US-TX-AUS'])
        assert is_super_admin(user)


# ---------------------------------------------------------------------------
# The queryset half
# ---------------------------------------------------------------------------

class TestQuerysetsAreFilteredToo:
    """A correct permission check with an unfiltered queryset still hands
    over every row in the world."""

    def test_an_india_admin_sees_only_indian_nodes(self, geo):
        user = make_admin('india2@saaradhigo.test', '+918800000020')
        assign(user, 'city_ops_manager', geo['IN'])

        visible = scope_queryset(user, GeoNode.objects.all(), 'path')
        codes = set(visible.values_list('code', flat=True))

        assert 'IN-TG-HYD' in codes
        assert 'US-TX-AUS' not in codes
        assert 'GLOBAL' not in codes

    def test_a_city_admin_sees_only_their_city(self, geo):
        user = make_admin('hyd2@saaradhigo.test', '+918800000021')
        assign(user, 'city_ops_manager', geo['IN-TG-HYD'])

        codes = set(
            scope_queryset(user, GeoNode.objects.all(), 'path')
            .values_list('code', flat=True)
        )

        assert codes == {'IN-TG-HYD'}

    def test_no_assignment_means_no_rows(self, geo):
        """Fails safe: a new operator sees nothing until granted something,
        rather than seeing everything until restricted."""
        user = make_admin('nobody2@saaradhigo.test', '+918800000022')

        assert scope_queryset(user, GeoNode.objects.all(), 'path').count() == 0

    def test_a_superuser_is_unrestricted(self, geo):
        user = make_admin('root@saaradhigo.test', '+918800000023')
        user.is_superuser = True
        user.save(update_fields=['is_superuser'])

        visible = scope_queryset(user, GeoNode.objects.all(), 'path')

        assert visible.count() == GeoNode.objects.count()

    def test_two_scopes_union(self, geo):
        user = make_admin('two@saaradhigo.test', '+918800000024')
        assign(user, 'city_ops_manager', geo['IN-TG-HYD'])
        assign(user, 'city_ops_manager', geo['US-TX-AUS'])

        codes = set(
            scope_queryset(user, GeoNode.objects.all(), 'path')
            .values_list('code', flat=True)
        )

        assert codes == {'IN-TG-HYD', 'US-TX-AUS'}


# ---------------------------------------------------------------------------
# PII
# ---------------------------------------------------------------------------

class TestPii:
    def test_an_analyst_never_sees_pii(self, geo):
        user = make_admin('analyst@saaradhigo.test', '+918800000030')
        assign(user, 'analyst', geo['IN-TG-HYD'])

        assert not can_see_pii(user, geo['IN-TG-HYD'])

    def test_a_safety_officer_can_unmask_in_their_scope(self, geo):
        user = make_admin('safety@saaradhigo.test', '+918800000031')
        assign(user, 'safety_officer', geo['IN-TG-HYD'])

        assert can_see_pii(user, geo['IN-TG-HYD'])
        # But not in another country.
        assert not can_see_pii(user, geo['US-TX-AUS'])


# ---------------------------------------------------------------------------
# The Super Admin floor
# ---------------------------------------------------------------------------

class TestTheSuperAdminFloor:
    def test_it_is_a_guard_not_a_bootstrap_requirement(self, geo):
        """A fresh environment may run with one, with a warning. Forcing a
        test account up to Global to satisfy a counter would be worse."""
        user = make_admin('solo@saaradhigo.test', '+918800000040')
        assign(user, SUPER_ADMIN, geo['GLOBAL'])

        assert super_admin_shortfall() == 1

    def test_the_shortfall_clears_at_two(self, geo):
        for i in (1, 2):
            user = make_admin(f'sa{i}@saaradhigo.test', f'+91880000004{i}')
            assign(user, SUPER_ADMIN, geo['GLOBAL'])

        assert super_admin_shortfall() == 0

    def test_revoking_below_the_floor_is_blocked(self, geo):
        assignments = []
        for i in (1, 2):
            user = make_admin(f'sb{i}@saaradhigo.test', f'+91880000005{i}')
            assignments.append(assign(user, SUPER_ADMIN, geo['GLOBAL']))

        with pytest.raises(WouldLeaveTooFewSuperAdmins):
            revoke_assignment(assignments[0])

    def test_an_environment_already_below_the_floor_can_still_revoke(self, geo):
        """Otherwise a mistake made at one Super Admin could never be undone."""
        user = make_admin('solo2@saaradhigo.test', '+918800000060')
        only = assign(user, SUPER_ADMIN, geo['GLOBAL'])

        revoke_assignment(only)

        only.refresh_from_db()
        assert only.is_active is False

    def test_revoking_a_non_super_admin_is_unaffected(self, geo):
        for i in (1, 2):
            u = make_admin(f'sc{i}@saaradhigo.test', f'+91880000007{i}')
            assign(u, SUPER_ADMIN, geo['GLOBAL'])
        ops = make_admin('ops@saaradhigo.test', '+918800000079')
        ops_assignment = assign(ops, 'city_ops_manager', geo['IN-TG-HYD'])

        revoke_assignment(ops_assignment)

        ops_assignment.refresh_from_db()
        assert ops_assignment.is_active is False


# ---------------------------------------------------------------------------
# The seed
# ---------------------------------------------------------------------------

class TestTheSeed:
    def test_it_assigns_nobody(self, geo):
        """Granting a human access is a decision with a name attached."""
        assert AdminAssignment.objects.count() == 0

    def test_it_is_idempotent(self, geo):
        from django.core.management import call_command

        before = GeoNode.objects.count()
        call_command('seed_admin_access')

        assert GeoNode.objects.count() == before

    def test_it_does_not_relaunch_a_paused_city(self, geo):
        """Pausing a city is an operational decision a redeploy must not undo."""
        from django.core.management import call_command

        hyderabad = GeoNode.objects.get(code='IN-TG-HYD')
        hyderabad.status = 'paused'
        hyderabad.save()

        call_command('seed_admin_access')

        assert GeoNode.objects.get(code='IN-TG-HYD').status == 'paused'

    def test_test_admins_are_refused_in_production(self, geo, settings):
        from django.core.management import call_command
        from django.core.management.base import CommandError

        settings.IS_PRODUCTION = True

        with pytest.raises(CommandError, match='production'):
            call_command('seed_admin_access', '--test-admins')

    def test_test_admins_cover_every_role_except_super_admin(self, geo):
        """One test login per role, and deliberately NO test Super Admin.

        A seeded Super Admin made `list_admin_access` report "the floor is
        met" on QA while the second real one was still unnamed -- the
        warning defeated by its own fixture. Super Admin is exercised
        through the real named account, which is also the only one that
        will hold it in production.
        """
        from django.core.management import call_command

        call_command('seed_admin_access', '--test-admins')

        roles = set(
            AdminAssignment.objects.values_list('role__code', flat=True)
        )
        assert SUPER_ADMIN not in roles
        assert roles == {t['code'] for t in ROLE_TEMPLATES} - {SUPER_ADMIN}
        # And none of them can be signed into with a guessable password.
        for assignment in AdminAssignment.objects.select_related('admin'):
            assert not assignment.admin.has_usable_password()

    def test_the_seed_cannot_satisfy_the_super_admin_floor(self, geo):
        """The property that matters, stated directly."""
        from django.core.management import call_command

        call_command('seed_admin_access', '--test-admins')

        assert super_admin_shortfall() == MIN_SUPER_ADMINS


# ---------------------------------------------------------------------------
# Break-glass does not count toward the floor
# ---------------------------------------------------------------------------

class TestBreakGlassDoesNotCountAsAPerson:
    """The floor exists to guarantee two ACCOUNTABLE PEOPLE.

    A shared account whose credentials sit in an envelope is not a second
    person, and counting it would let the floor be satisfied by a key under
    glass -- the one case it exists to rule out.
    """

    def _super_admin(self, email, phone, geo, *, break_glass=False):
        user = User.objects.create_user(
            phone_number=phone, username=phone, email=email,
            role='admin', is_staff=True,
        )
        if break_glass:
            user.is_break_glass = True
            user.save(update_fields=['is_break_glass'])
        assign(user, SUPER_ADMIN, geo['GLOBAL'])
        return user

    def test_one_named_plus_break_glass_is_still_one_short(self, geo):
        self._super_admin('named@saaradhigo.test', '+918800009001', geo)
        self._super_admin('glass@saaradhigo.test', '+918800009002', geo,
                          break_glass=True)

        assert super_admin_shortfall() == 1

    def test_two_named_meets_the_floor(self, geo):
        self._super_admin('n1@saaradhigo.test', '+918800009003', geo)
        self._super_admin('n2@saaradhigo.test', '+918800009004', geo)

        assert super_admin_shortfall() == 0

    def test_break_glass_is_not_counted_even_alone(self, geo):
        self._super_admin('only@saaradhigo.test', '+918800009005', geo,
                          break_glass=True)

        assert super_admin_shortfall() == MIN_SUPER_ADMINS

    def test_revoking_a_named_one_is_not_blocked_by_the_glass_account(self, geo):
        """Two named plus the emergency account: revoking one named leaves
        one named, which is below the floor, so it must still be refused --
        the emergency account must not be what makes it look safe."""
        a = self._super_admin('r1@saaradhigo.test', '+918800009006', geo)
        self._super_admin('r2@saaradhigo.test', '+918800009007', geo)
        self._super_admin('rg@saaradhigo.test', '+918800009008', geo,
                          break_glass=True)

        assignment = AdminAssignment.objects.get(admin=a)
        with pytest.raises(WouldLeaveTooFewSuperAdmins):
            revoke_assignment(assignment)

    def test_the_glass_account_still_holds_the_authority(self, geo):
        """Excluded from the COUNT, not from the role. In the emergency it
        is supposed to work."""
        user = self._super_admin('works@saaradhigo.test', '+918800009009',
                                 geo, break_glass=True)

        assert is_super_admin(user)
        assert has_permission_at(user, 'finance.payout', geo['US-TX-AUS'])
