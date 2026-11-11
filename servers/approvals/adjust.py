"""One finance.adjust executor, dispatched by target type.

The registry holds one executor per action key. finance.adjust began as the
trip fare adjustment; the driver balance adjustment (Part B) is a second
thing under the same key and the same per-country policy ("manual ledger
adjustment, maker alone up to X"). So the executor registered for the key is
this dispatcher, and each module registers its runner under the target_type
its requests carry ('trip', 'driver_balance'), exactly as bulk.action does.
"""

from . import registry
from .actions import FINANCE_ADJUST

_RUNNERS = {}


def register(target_type):
    """Decorator: @adjust.register('driver_balance') on a runner."""
    def wrap(fn):
        if target_type in _RUNNERS and _RUNNERS[target_type] is not fn:
            raise RuntimeError(f'two adjustment runners registered for {target_type!r}')
        _RUNNERS[target_type] = fn
        return fn
    return wrap


def execute_adjust(request_row):
    runner = _RUNNERS.get(request_row.target_type)
    if runner is None:
        # A worker that never imported the module owning the runner.
        import importlib

        importlib.import_module('servers.ride.admin_trips')
        runner = _RUNNERS.get(request_row.target_type)
    if runner is None:
        raise registry.ExecutionRefused(
            'UNKNOWN_ADJUST_TARGET', f'No adjustment runner for {request_row.target_type!r}.')
    return runner(request_row)


registry.register(FINANCE_ADJUST)(execute_adjust)
