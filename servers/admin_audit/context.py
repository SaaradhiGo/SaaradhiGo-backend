"""Who is making the access change that is about to be audited.

WHY A CONTEXT AND NOT AN ARGUMENT
---------------------------------
Access rows (`AdminAssignment`, `AdminRole`, `GeoNode`, the authority flags
on a user) are audited by model signals -- see `admin_audit.access_audit`
-- so that a management command, a shell or a seed cannot change who may do
what without leaving a row. A signal receiver sees the row, not the caller:
it cannot know whether the save came from an operator in a view, from
`migrate_operators`, or from somebody typing into `manage.py shell`.

So the caller says so here, once, around the block of work:

    with audit_context(actor=request.user, reason='Quarterly access review'):
        assignment.is_active = False
        assignment.save()

and every structural row written inside that block carries that actor and
reason. The middleware in `admin_audit.middleware` does this for every HTTP
request, so a view normally needs to do nothing.

WHEN NOTHING IS SET, THE ROW IS STILL WRITTEN
--------------------------------------------
With no context the receiver writes the row anyway, with `actor=None` and
the label `unattributed write via <model>`. An unattributed row is a gap
you can see and go and fix; a missing row is a gap nobody finds until the
access review that needed it. The one thing a context can never do is make
the receiver skip the row.

THE ACTOR IS RESOLVED LATE
--------------------------
A request's user is not known when middleware runs: DRF authenticates a
bearer token inside the view and then sets `request.user` on the underlying
`HttpRequest`. Storing the request and reading `.user` at the moment the
row is written is what makes API writes come out attributed.

`contextvars` rather than thread-locals, so the value follows the request
through `sync_to_async` under ASGI and is isolated per task, not per thread.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

_current: ContextVar = ContextVar('admin_audit_context', default=None)


@dataclass
class AuditContext:
    """What the receivers know about the caller of an access write."""

    #: The user making the change, or None.
    actor: object = None
    #: A label that survives the actor's deletion; derived from the actor
    #: when empty.
    actor_label: str = ''
    #: Why. Copied into every structural row written inside the block.
    reason: str = ''
    #: The HTTP request, when there is one: the actor and the client
    #: address are read from it at write time.
    request: object = None
    #: A business-level action name to put on the row INSTEAD of the
    #: structural default (`operator_created` rather than
    #: `operator_account_created`). The row is still written; only its
    #: name changes. This is how a caller that used to write its own row
    #: for the same change stops writing a second one.
    action: str = ''
    #: Extra keys merged into `after` on every row written in the block,
    #: for facts the caller knows and the model does not (whether MFA
    #: enrolment was started, which environment this is).
    extra_after: dict = field(default_factory=dict)
    #: Rows written by the receivers while this context was current, in
    #: order. Lets a caller see whether its write produced one.
    rows: list = field(default_factory=list)

    def resolve_actor(self):
        if self.actor is not None:
            return self.actor
        user = getattr(self.request, 'user', None)
        if user is not None and getattr(user, 'is_authenticated', False):
            return user
        return None


def current_context():
    """The innermost active `AuditContext`, or None."""
    return _current.get()


@contextmanager
def audit_context(actor=None, actor_label='', reason='', request=None,
                  action='', extra_after=None):
    """Attribute every access write inside the block.

    Nests: an inner block wins for its duration, and the outer one is
    restored on exit, so a command that sets a coarse context can still
    wrap one write in a more specific one.
    """
    ctx = AuditContext(
        actor=actor, actor_label=actor_label, reason=reason, request=request,
        action=action, extra_after=dict(extra_after or {}),
    )
    token = _current.set(ctx)
    try:
        yield ctx
    finally:
        _current.reset(token)
