"""One `bulk.action` executor for every module, dispatched by target type.

The registry holds one executor per action key, and `bulk.action` is one
key shared by every module's bulk endpoint (one threshold: "bulk actions
over 50 records always need a checker"). So the executor registered for it
is this dispatcher, and each module registers its own bulk runner here
under the `target_type` its requests carry (`trip_bulk`, `driver_bulk`).
A request whose target type nobody registered fails, recorded on the row.
"""

from . import registry
from .actions import BULK_ACTION

_RUNNERS = {}


def register(target_type):
    """Decorator: `@bulk.register('driver_bulk')` on a module's bulk runner."""
    def wrap(fn):
        if target_type in _RUNNERS and _RUNNERS[target_type] is not fn:
            raise RuntimeError(f'two bulk runners registered for {target_type!r}')
        _RUNNERS[target_type] = fn
        return fn
    return wrap


def execute_bulk(request_row):
    runner = _RUNNERS.get(request_row.target_type)
    if runner is None:
        raise registry.ExecutionRefused(
            'UNKNOWN_BULK_TARGET', f'No bulk runner for {request_row.target_type!r}.')
    return runner(request_row)


registry.register(BULK_ACTION)(execute_bulk)
