"""Who may do what, where.

THE SHAPE, AND WHY IT IS THIS SHAPE
-----------------------------------
SaaradhiGo launches in India and the USA, so admin authority is never
global by default. Three pieces:

    GeoNode          Global → Country → State/Region → City
    AdminRole        an editable set of permissions
    AdminAssignment  (admin, role, scope) — an admin may hold several

An assignment at a node covers that node and **everything beneath it**. A
Country Ops Manager for India sees Telangana and Hyderabad without anybody
listing them.

WHY STATE IS NOT OPTIONAL
-------------------------
US licensing, insurance, background-check and tax rules are set at state
level. A city-only model — which is what this replaced — would have had to
be torn up for the US launch, and every module built on it with it.

WHY A MATERIALISED PATH
-----------------------
"Does this assignment cover that city?" is asked on every request and in
every queryset. With a parent pointer alone it is a recursive query each
time. `path` stores `/IN/IN-TG/IN-TG-HYD/`, so coverage is a `startswith`
on an indexed column, and a queryset can be filtered by the union of an
admin's scopes with one `OR` of prefixes.

The cost is that moving a node has to rewrite its subtree's paths. Cities
do not move between countries, and `rebuild_paths` exists for the day a
region is reorganised.

WHY ROLES ARE DATA AND THERE ARE NO PER-USER OVERRIDES
------------------------------------------------------
"Who can approve a payout in Texas" must be answerable by reading a role
and its assignments. One per-user exception makes that answer a search
through every account, and the exception is always the one nobody
remembers. Super Admin may create custom roles; it may not grant a single
person an extra permission.
"""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction

from servers.admin_audit.access_audit import AccessAuditedManager, AccessAuditedQuerySet

from .roles import SUPER_ADMIN

# EVERY WRITE HERE IS AUDITED, AND NOT BY THE CALLER
# --------------------------------------------------
# A row in this module decides who may approve a payout or read a licence.
# `servers.admin_audit.access_audit` connects signal receivers to all three
# models so that any create, update or delete -- from a view, a management
# command, a seed or a shell -- appends an `AdminAuditLog` row describing
# the change. `AccessAuditedManager` extends that to `update()`,
# `bulk_update()` and `bulk_create()`, which send no signals. The `save()`
# methods below run inside `transaction.atomic()` so the write and its
# audit row commit together or not at all.
#
# Found necessary on 2026-10-27, when `migrate_operators --apply` granted
# Super Admin on QA and left no row. See the docstring of `access_audit`.

#: Node kinds, outermost first. The index is the depth.
GLOBAL, COUNTRY, STATE, CITY = 'global', 'country', 'state', 'city'

KIND_CHOICES = [
    (GLOBAL, 'Global'),
    (COUNTRY, 'Country'),
    (STATE, 'State or region'),
    (CITY, 'City'),
]

#: Which kind may contain which. Enforced on save, because a city under a
#: country with no state would quietly break every state-level rule the US
#: needs.
ALLOWED_PARENT = {
    GLOBAL: None,
    COUNTRY: GLOBAL,
    STATE: COUNTRY,
    CITY: STATE,
}

#: `active` is what the console calls LIVE. The stored value is unchanged (a dozen readers
#: filter on it); `draft` is a city that has never been launched, `paused` one that was live.
#: draft -> live -> paused -> live. Only `readiness.set_city_status` moves a city (module 15).
DRAFT, ACTIVE, PAUSED = 'draft', 'active', 'paused'
STATUS_CHOICES = [(DRAFT, 'Draft'), (ACTIVE, 'Live'), (PAUSED, 'Paused')]


class GeoNode(models.Model):
    """One place in the hierarchy.

    `code` is stable and human-readable (`IN`, `IN-TG`, `IN-TG-HYD`) so it
    can appear in config, seeds and URLs without exposing primary keys.
    """

    kind = models.CharField(max_length=10, choices=KIND_CHOICES, db_index=True)
    code = models.CharField(max_length=32, unique=True)
    name = models.CharField(max_length=120)
    parent = models.ForeignKey(
        'self', null=True, blank=True, on_delete=models.PROTECT,
        related_name='children',
    )
    # `/IN/IN-TG/IN-TG-HYD/`, maintained on save. Indexed for prefix search.
    path = models.CharField(max_length=255, db_index=True, editable=False, default='')
    # A city is launched or paused. The brief's "launch/pause a city" lives
    # here rather than in a feature flag, because it is a property of the
    # place and reporting needs its history.
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=DRAFT)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = AccessAuditedManager()

    class Meta:
        ordering = ['path']
        indexes = [models.Index(fields=['kind', 'status'], name='geonode_kind_status_idx')]

    def __str__(self):
        return f'{self.name} ({self.code})'

    def clean(self):
        expected = ALLOWED_PARENT[self.kind]
        actual = self.parent.kind if self.parent else None
        if expected != actual:
            raise ValidationError(
                f'A {self.get_kind_display().lower()} must sit under '
                f'{expected or "nothing"}, not {actual or "nothing"}.'
            )

    def build_path(self):
        return f'{self.parent.path if self.parent else ""}{self.code}/'

    def save(self, *args, **kwargs):
        self.full_clean(exclude=['path'])
        self.path = self.build_path()
        with transaction.atomic():
            super().save(*args, **kwargs)
            # A rename or re-parent must not leave descendants pointing at
            # the old prefix; they would silently fall outside every scope
            # check.
            for child in self.children.all():
                child.save()

    def covers(self, other):
        """Does this node contain `other` (or is it `other`)?"""
        return bool(other) and str(other.path).startswith(self.path)

    @property
    def country(self):
        """The country this node belongs to, or None at Global."""
        node = self
        while node and node.kind != COUNTRY:
            node = node.parent
        return node


class AdminRole(models.Model):
    """An editable set of permissions.

    `is_template` marks the eight roles seeded from the brief. They can be
    edited like any other, but they are recreated by the seed command, so a
    deployment that loses its database comes back with a sane default set.
    """

    code = models.SlugField(max_length=64, unique=True)
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    #: Permission strings, e.g. `trip.read`. The catalogue is in roles.py.
    permissions = models.JSONField(default=list, blank=True)
    #: Global-only roles may only be assigned at the Global node.
    is_global_only = models.BooleanField(default=False)
    is_template = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = AccessAuditedManager()

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name

    def grants(self, permission):
        return permission in (self.permissions or [])

    def save(self, *args, **kwargs):
        with transaction.atomic():
            super().save(*args, **kwargs)


#: The `code` on the ValidationError raised for a non-Super-Admin role at
#: Global, so a caller can tell this refusal from any other.
GLOBAL_IS_SUPER_ADMIN_ONLY = 'GLOBAL_IS_SUPER_ADMIN_ONLY'


def check_role_at_scope(role, scope):
    """Refuse a (role, scope) pair the access model does not allow.

    GLOBAL IS UNRESTRICTED, SO ONLY SUPER ADMIN MAY HOLD IT
    -------------------------------------------------------
    `access.scope_paths` treats ANY assignment at the Global node as
    "unrestricted": the root of the tree covers everything, including rows
    that sit under no node at all. That is the right reading of the tree,
    and it means a Support Agent assigned at Global would quietly become a
    Support Agent for every country, every city and every unanchored row.
    The owner's ruling (2026-10-28): accepted, but only the `super_admin`
    role may ever be assigned at Global. Every other role is held somewhere
    -- a country, a state, a city -- which is what stops an India hire
    quietly acquiring the USA.

    Enforced here, in code, rather than as a database CheckConstraint: the
    rule joins two tables (the role's code and the node's kind), which a
    check constraint cannot express. `AdminAssignment.save()`,
    `AdminAssignmentQuerySet.update()` and `.bulk_create()` all pass
    through this, so there is no ORM path that writes such a row.
    `migrate_operators` refuses a plan line before `--apply` as well.
    """
    if scope.kind == GLOBAL and role.code != SUPER_ADMIN:
        raise ValidationError(
            f'Only {SUPER_ADMIN} may be assigned at Global; {role.code} '
            f'must be held at a country, state or city.',
            code=GLOBAL_IS_SUPER_ADMIN_ONLY,
        )
    if role.is_global_only and scope.kind != GLOBAL:
        raise ValidationError(
            f'{role.name} may only be held globally, not at {scope.code}.',
            code='GLOBAL_ONLY_ROLE',
        )


class AdminAssignmentQuerySet(AccessAuditedQuerySet):
    """The audited queryset, plus the Global rule on the paths `save()` misses.

    `update()` and `bulk_create()` bypass `full_clean()`. An assignment
    moved to Global with `.filter(...).update(scope=global_node)` would be
    the one way to mint an unrestricted non-Super-Admin, so both are
    checked row by row before anything is written.
    """

    def _resolve(self, kwargs, name, model):
        if name in kwargs:
            value = kwargs[name]
            return value if isinstance(value, model) else model.objects.get(pk=value)
        if f'{name}_id' in kwargs:
            return model.objects.get(pk=kwargs[f'{name}_id'])
        return None

    def update(self, **kwargs):
        if any(key in kwargs for key in ('role', 'role_id', 'scope', 'scope_id')):
            new_role = self._resolve(kwargs, 'role', AdminRole)
            new_scope = self._resolve(kwargs, 'scope', GeoNode)
            for row in self._chain().select_related('role', 'scope'):
                check_role_at_scope(new_role or row.role, new_scope or row.scope)
        return super().update(**kwargs)

    update.alters_data = True

    def bulk_create(self, objs, *args, **kwargs):
        for obj in objs:
            check_role_at_scope(obj.role, obj.scope)
        return super().bulk_create(objs, *args, **kwargs)

    bulk_create.alters_data = True


class AdminAssignmentManager(AccessAuditedManager.from_queryset(AdminAssignmentQuerySet)):
    """`use_in_migrations` stays False, as on the parent: behaviour, not schema."""


class AdminAssignment(models.Model):
    """`admin` holds `role` over `scope` and everything beneath it.

    Only `super_admin` may be held at Global -- see `check_role_at_scope`.
    """

    admin = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='admin_assignments',
    )
    role = models.ForeignKey(AdminRole, on_delete=models.PROTECT,
                             related_name='assignments')
    scope = models.ForeignKey(GeoNode, on_delete=models.PROTECT,
                              related_name='assignments')
    is_active = models.BooleanField(default=True)

    # Who granted this, and why. An assignment is itself a privileged act.
    granted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='admin_assignments_granted',
    )
    reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = AdminAssignmentManager()

    class Meta:
        ordering = ['admin_id', 'scope__path']
        constraints = [
            models.UniqueConstraint(
                fields=['admin', 'role', 'scope'],
                name='admin_access_unique_assignment',
            ),
        ]

    def __str__(self):
        return f'{self.admin_id}: {self.role.code} @ {self.scope.code}'

    def clean(self):
        if self.role_id and self.scope_id:
            check_role_at_scope(self.role, self.scope)

    def save(self, *args, **kwargs):
        self.full_clean(exclude=['granted_by', 'reason'])
        with transaction.atomic():
            super().save(*args, **kwargs)
