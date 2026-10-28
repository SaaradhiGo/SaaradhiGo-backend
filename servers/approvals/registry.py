"""Which code runs when a request is approved.

An `ApprovalRequest` row outlives the HTTP request that raised it, so the
work to do on approval cannot be a closure: it has to be found again, hours
later, from the action key stored on the row. Modules register one executor
per action key, and `service.approve` looks it up by that key.

Registration is import-time (`apps.ApprovalsConfig.ready` imports
`executors`), so a key with no executor is detected the moment a maker
tries to raise it (`service` refuses before creating the row) rather than
when a checker tries to approve it.

An executor receives the `ApprovalRequest` and returns a JSON-serialisable
dict that is stored on the row as `result`. Raising `ExecutionRefused`
marks the request `failed` with the given code; any other exception is
also recorded as `failed`, with the exception text, and re-logged.
"""

_EXECUTORS = {}


class NoExecutor(Exception):
    """No module has registered code for this action key."""


class ExecutionRefused(Exception):
    """The executor looked at the world and declined to act.

    For the case where the target changed between request and approval:
    a withdrawal that is no longer pending, a driver already deactivated.
    The request is recorded as `failed` with `code`, and nothing moved.
    """

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def register(action_key):
    """Decorator: `@register('finance.payout')` on the executor function."""
    def wrap(fn):
        if action_key in _EXECUTORS and _EXECUTORS[action_key] is not fn:
            raise RuntimeError(
                f'two executors registered for {action_key!r}: '
                f'{_EXECUTORS[action_key]!r} and {fn!r}'
            )
        _EXECUTORS[action_key] = fn
        return fn
    return wrap


def executor_for(action_key):
    try:
        return _EXECUTORS[action_key]
    except KeyError:
        raise NoExecutor(
            f'no executor is registered for {action_key!r}; a request for it '
            f'could be raised but never carried out'
        ) from None


def has_executor(action_key):
    return action_key in _EXECUTORS


def registered_actions():
    return sorted(_EXECUTORS)
