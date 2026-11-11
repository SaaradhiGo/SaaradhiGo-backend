"""Every write to an access row leaves an audit row. Enforced, not requested.

THE GAP THIS CLOSES
-------------------
On 2026-10-27 `migrate_operators --plan qa --apply` gave one account Super
Admin at Global, another City Ops Manager at Hyderabad, and flagged a third
as the break-glass account -- and wrote nothing to `AdminAuditLog`. Nothing
was wrong with the command by the rules then in force: audit rows were
written by views that chose to call `record_admin_action`, and a management
command is not a view. That rule meant the log recorded the changes made
through the front door and none of the ones made through the side.

Who holds authority over money and identity documents is the one thing an
access review needs the log for. So the rule changes: an access row cannot
be written without an audit row, from anywhere -- a view, a command, a
seed, a shell, a migration's RunPython. Enforced at the model layer, where
every path converges, rather than requested of every caller.

WHAT COUNTS AS AN ACCESS ROW
----------------------------
`ACCESS_MODELS` is the one list. A test enumerates every model in
`servers.admin_access` and fails if one is missing from it, so a new model
in that app cannot be added without a decision about auditing it.

  * `AdminAssignment` -- who holds which role, where. The authority itself.
  * `AdminRole`       -- what a role may do. Editing one edits everybody
                         holding it.
  * `GeoNode`         -- what a scope covers. Re-parenting a node moves
                         every assignment at it.
  * the user's authority flags -- `is_staff`, `is_superuser`, `role`,
    `ops_role`, `is_break_glass`. `base.permissions.is_operator` decides
    who may operate from the first three; `ops_role` is the legacy
    authority the bridge still honours; the break-glass flag changes how
    sign-in behaves. A rider's profile edits are not access, so for the
    user model ONLY these fields are compared, and creates and deletes are
    recorded only for accounts that are operator-shaped.

HOW IT IS DONE
--------------
`pre_save` fetches the row as the database has it now; `post_save` compares
it with what was written and records the difference. Only a REAL change is
recorded: `seed_admin_access` re-saves every node and role on every deploy,
and eleven "nothing changed" rows per deploy would be noise that trains
people to stop reading the log. `post_delete` records what was removed.

`QuerySet.update()` and `bulk_create()` bypass signals, so the access
models carry a queryset whose versions of those methods record rows
themselves (snapshot, write, snapshot, diff). `bulk_update()` is built on
`update()` and is covered by it. `QuerySet.delete()` needs nothing extra:
once a `post_delete` receiver is connected Django abandons the fast path
and sends the signal per row.

The receiver DOES NOT swallow failures. `record_admin_action` does, because
an audit problem must not stop a KYC approval. Here the opposite holds: an
access change that cannot be recorded must not happen quietly. The models'
`save()` runs inside `transaction.atomic()`, so the write and its row
commit together or not at all.

WHAT IT CANNOT SEE
------------------
Raw SQL, `QuerySet.update()` on a model that is not in the list, and a
migration operating on historical models (which have no signals and plain
managers). Those are the paths of somebody with database credentials, and
the hash chain exists for that case: it cannot stop them, it makes them
detectable. See `hashchain.py`.

WHO DID IT
----------
From `admin_audit.context`. With no context the row is written with
`actor=None` and the label `unattributed write via <model>`, which is a gap
you can see. The context can shape the row; nothing can suppress it.
"""

import logging
from dataclasses import dataclass, field

from django.db import models, transaction
from django.db.models.signals import post_delete, post_save, pre_save

from .context import current_context

logger = logging.getLogger(__name__)

#: Sentinel: this save cannot have changed a tracked field, so post_save
#: has nothing to compare.
_NOTHING_TRACKED = object()
_BEFORE_ATTR = '_access_audit_before'
_DISPATCH_PREFIX = 'admin_audit.access_audit'


# ---------------------------------------------------------------------------
# What is audited, and how each model is described
# ---------------------------------------------------------------------------

def _snapshot_geo_node(node):
    return {
        'kind': node.kind,
        'code': node.code,
        'name': node.name,
        'parent_id': node.parent_id,
        'path': node.path,
        'status': node.status,
    }


def _snapshot_role(role):
    return {
        'code': role.code,
        'name': role.name,
        'description': role.description,
        'permissions': sorted(role.permissions or []),
        'is_global_only': bool(role.is_global_only),
        'is_template': bool(role.is_template),
        'is_active': bool(role.is_active),
    }


def _code_of(instance, attr):
    """`instance.role.code` without failing when the related row is gone."""
    try:
        related = getattr(instance, attr)
    except Exception:  # noqa: BLE001 - PROTECT makes this rare; ids still tell the story
        related = None
    return getattr(related, 'code', None)


def _snapshot_assignment(assignment):
    return {
        'admin_id': assignment.admin_id,
        'role_id': assignment.role_id,
        'role': _code_of(assignment, 'role'),
        'scope_id': assignment.scope_id,
        'scope': _code_of(assignment, 'scope'),
        'is_active': bool(assignment.is_active),
        'granted_by_id': assignment.granted_by_id,
        'reason': assignment.reason,
    }


def _snapshot_user_authority(user):
    return {
        'is_staff': bool(user.is_staff),
        'is_superuser': bool(user.is_superuser),
        'role': user.role,
        'ops_role': user.ops_role,
        'is_break_glass': bool(user.is_break_glass),
    }


def _is_operator_shaped(snapshot):
    return bool(snapshot['is_staff'] or snapshot['is_superuser']
                or snapshot['role'] == 'admin')


@dataclass(frozen=True)
class AccessModelSpec:
    """How one access model is audited."""

    #: `app_label.ModelName`, resolved at `connect()` time.
    label: str
    #: `target_type` on the audit row.
    target_type: str
    #: Actions for create / update / delete.
    created_action: str
    updated_action: str
    deleted_action: str
    #: Model fields whose change matters. A save with `update_fields`
    #: disjoint from these is skipped without a query.
    tracked: frozenset
    #: The JSON-safe description of a row that is compared and stored.
    snapshot: callable
    #: Relations the snapshot reads, fetched with the before-image.
    select_related: tuple = ()
    #: Whether a created or deleted row is worth recording at all. Every
    #: assignment is; a rider account is not.
    matters: callable = field(default=lambda snapshot: True)

    def fetch(self, pk):
        """The row as the database holds it now, or None."""
        from django.apps import apps

        model = apps.get_model(self.label)
        return (model._base_manager.select_related(*self.select_related)
                .filter(pk=pk).first())

    def prefetch(self, queryset):
        return queryset.select_related(*self.select_related) if self.select_related else queryset


#: THE list. `tests/test_access_writes_are_audited.py` checks that every
#: model in `servers.admin_access` appears here.
ACCESS_MODELS = (
    AccessModelSpec(
        label='admin_access.GeoNode', target_type='GeoNode',
        created_action='access_scope_created',
        updated_action='access_scope_updated',
        deleted_action='access_scope_deleted',
        tracked=frozenset({'kind', 'code', 'name', 'parent', 'path', 'status'}),
        snapshot=_snapshot_geo_node,
    ),
    AccessModelSpec(
        label='admin_access.AdminRole', target_type='AdminRole',
        created_action='access_role_created',
        updated_action='access_role_updated',
        deleted_action='access_role_deleted',
        tracked=frozenset({'code', 'name', 'description', 'permissions',
                           'is_global_only', 'is_template', 'is_active'}),
        snapshot=_snapshot_role,
    ),
    AccessModelSpec(
        label='admin_access.AdminAssignment', target_type='AdminAssignment',
        created_action='access_assignment_created',
        updated_action='access_assignment_updated',
        deleted_action='access_assignment_deleted',
        tracked=frozenset({'admin', 'role', 'scope', 'is_active',
                           'granted_by', 'reason'}),
        snapshot=_snapshot_assignment,
        select_related=('role', 'scope'),
    ),
    AccessModelSpec(
        label='auth_user.customUser', target_type='User',
        created_action='operator_account_created',
        updated_action='operator_authority_changed',
        deleted_action='operator_account_deleted',
        tracked=frozenset({'is_staff', 'is_superuser', 'role', 'ops_role',
                           'is_break_glass'}),
        snapshot=_snapshot_user_authority,
        matters=_is_operator_shaped,
    ),
)

_SPEC_BY_LABEL = {spec.label: spec for spec in ACCESS_MODELS}


def spec_for(model):
    """The spec for a model class, or None if it is not an access model."""
    return _SPEC_BY_LABEL.get(model._meta.label)


def _tracked_touched(spec, names):
    """Could a write naming these fields change anything tracked?

    `update_fields` and `update()` kwargs name fields; the tracked set
    names fields too, but a FK may be given as `role` or `role_id`.
    """
    plain = {str(name).removesuffix('_id') for name in names}
    return bool(plain & spec.tracked)


# ---------------------------------------------------------------------------
# Writing the row
# ---------------------------------------------------------------------------

def _actor_label(actor):
    from .services import _actor_label as label_for

    return label_for(actor)


def _changed_fields(before, after):
    return sorted(key for key in set(before) | set(after)
                  if before.get(key) != after.get(key))


def record_access_change(spec, target_id, action, before, after, *,
                         changed=None):
    """Append one audit row for a structural access change.

    Raises on failure, deliberately: see the module docstring. The caller's
    `save()` is inside `transaction.atomic()`, so a failure here takes the
    access write down with it.
    """
    from .models import AdminAuditLog
    from .services import _client_ip, _user_agent

    ctx = current_context()
    actor = ctx.resolve_actor() if ctx else None
    label = ''
    if ctx:
        label = ctx.actor_label or _actor_label(actor)
    if not label:
        label = f'unattributed write via {spec.target_type}'

    payload_after = dict(after or {})
    if changed:
        payload_after['changed_fields'] = list(changed)
    if ctx and ctx.extra_after:
        payload_after.update(ctx.extra_after)

    request = ctx.request if ctx else None
    row = AdminAuditLog.objects.create(
        actor=actor,
        actor_label=label[:255],
        action=(ctx.action if ctx and ctx.action else action),
        target_type=spec.target_type,
        target_id=str(target_id) if target_id is not None else '',
        before=dict(before or {}),
        after=payload_after,
        reason=(ctx.reason if ctx else '')[:10000],
        ip_address=_client_ip(request),
        user_agent=_user_agent(request),
    )
    if ctx is not None:
        ctx.rows.append(row)
    return row


def _record_created(spec, instance):
    after = spec.snapshot(instance)
    if not spec.matters(after):
        return None
    return record_access_change(spec, instance.pk, spec.created_action, {}, after)


def _record_updated(spec, pk, before, after):
    changed = _changed_fields(before, after)
    if not changed:
        return None
    return record_access_change(spec, pk, spec.updated_action, before, after,
                                changed=changed)


def _record_deleted(spec, instance):
    before = spec.snapshot(instance)
    if not spec.matters(before):
        return None
    return record_access_change(spec, instance.pk, spec.deleted_action, before, {})


# ---------------------------------------------------------------------------
# Signal receivers
# ---------------------------------------------------------------------------

def _on_pre_save(sender, instance, raw=False, update_fields=None, **kwargs):
    spec = spec_for(sender)
    if instance.pk is None:
        setattr(instance, _BEFORE_ATTR, None)
        return
    if update_fields is not None and not _tracked_touched(spec, update_fields):
        # `last_login`, `fcm_token`, a profile field: nothing here can
        # change authority, so no query is spent finding that out.
        setattr(instance, _BEFORE_ATTR, _NOTHING_TRACKED)
        return
    previous = spec.fetch(instance.pk)
    setattr(instance, _BEFORE_ATTR, spec.snapshot(previous) if previous else None)


def _on_post_save(sender, instance, created=False, raw=False,
                  update_fields=None, **kwargs):
    spec = spec_for(sender)
    before = instance.__dict__.pop(_BEFORE_ATTR, None)
    if before is _NOTHING_TRACKED:
        return
    if before is None:
        _record_created(spec, instance)
        return
    if update_fields is not None:
        # Only the named fields reached the database. The in-memory
        # instance may carry other edits that were NOT written, so the
        # after-image is read back rather than trusted.
        current = spec.fetch(instance.pk)
        after = spec.snapshot(current) if current else spec.snapshot(instance)
    else:
        after = spec.snapshot(instance)
    _record_updated(spec, instance.pk, before, after)


def _on_post_delete(sender, instance, **kwargs):
    _record_deleted(spec_for(sender), instance)


def connect():
    """Wire the receivers. Called from `AdminAuditConfig.ready()`.

    `dispatch_uid` makes a second call (tests reloading apps, a double
    `ready()`) a no-op rather than a second row per write.
    """
    from django.apps import apps

    for spec in ACCESS_MODELS:
        model = apps.get_model(spec.label)
        pre_save.connect(_on_pre_save, sender=model,
                         dispatch_uid=f'{_DISPATCH_PREFIX}.pre_save.{spec.label}')
        post_save.connect(_on_post_save, sender=model,
                          dispatch_uid=f'{_DISPATCH_PREFIX}.post_save.{spec.label}')
        post_delete.connect(_on_post_delete, sender=model,
                            dispatch_uid=f'{_DISPATCH_PREFIX}.post_delete.{spec.label}')


# ---------------------------------------------------------------------------
# The bulk paths that signals do not see
# ---------------------------------------------------------------------------

class AccessAuditedQuerySet(models.QuerySet):
    """A queryset whose bulk writes record what they changed.

    `update()` and `bulk_create()` send no signals. On an access model each
    one here snapshots the rows, performs the write and records the per-row
    difference, inside one transaction. On a model the registry does not
    know -- or for an `update()` that touches no tracked field -- they are
    the plain Django methods with no extra query.

    `bulk_update()` is deliberately NOT overridden: Django implements it as
    `self.filter(pk__in=...).update(**cases)`, which lands in `update()`
    below and is recorded there. An override here recorded every row twice.
    """

    def _spec(self):
        return spec_for(self.model)

    def update(self, **kwargs):
        spec = self._spec()
        if spec is None or not _tracked_touched(spec, kwargs):
            return super().update(**kwargs)
        with transaction.atomic(using=self.db):
            before = {obj.pk: spec.snapshot(obj)
                      for obj in spec.prefetch(self._chain())}
            count = super().update(**kwargs)
            after_rows = spec.prefetch(
                self.model._base_manager.using(self.db).filter(pk__in=list(before))
            )
            for obj in after_rows:
                _record_updated(spec, obj.pk, before[obj.pk], spec.snapshot(obj))
            return count

    update.alters_data = True

    def bulk_create(self, objs, *args, **kwargs):
        spec = self._spec()
        if spec is None:
            return super().bulk_create(objs, *args, **kwargs)
        with transaction.atomic(using=self.db):
            created = super().bulk_create(objs, *args, **kwargs)
            for obj in created:
                # A conflict ignored by `ignore_conflicts` comes back with
                # no primary key: nothing was written for it.
                if obj.pk is not None:
                    _record_created(spec, obj)
            return created

    bulk_create.alters_data = True


class AccessAuditedManager(models.Manager.from_queryset(AccessAuditedQuerySet)):
    """Default manager for the access models in `servers.admin_access`.

    `use_in_migrations` is left False: the manager changes behaviour, not
    schema, and must not generate a migration.
    """
