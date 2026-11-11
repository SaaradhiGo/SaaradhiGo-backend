"""Root test configuration.

Exists for one reason: the Channels channel layer is a process-wide singleton,
and the suite's async tests each run on their own event loop. Without the
fixture below, running the PostgreSQL-marked tests in a single process fails 22
of 97 -- including every trip-request idempotency test and most of the reconnect
state machine -- while each file passes on its own.

This lives at the repository root rather than in `tests/conftest.py` because the
suite has two test roots (`tests/` and `servers/**/tests/`, see pytest.ini) and
the problem belongs to neither in particular.

Note the deliberate limit of what a root conftest can do: pytest-django reads
`DATABASES` during `pytest_load_initial_conftests`, which is earlier than any
conftest runs. That is why test-time environment defaults live in
`base/settings_ci.py` and not here. Fixtures are fine; settings are not.
"""

import pytest


@pytest.fixture(autouse=True)
def isolate_channel_layer():
    """Give every test its own channel layer.

    `channels.layers.channel_layers` caches one RedisChannelLayer per alias for
    the life of the process. A RedisChannelLayer binds its connections and its
    pending futures to whichever event loop first touched it.

    Every async test here gets a fresh event loop. So the second async test in a
    process inherits a layer wired to the first test's now-dead loop, and
    channels_redis raises:

        RuntimeError: Two event loops are trying to receive() on one channel
        layer at once!

    It is made worse by consumer background tasks outliving their test --
    `LocationBroadcastMixin._location_pump` sits in `await queue.get()` and is
    reported as "Task was destroyed but it is pending" -- because such a task
    keeps a `receive()` outstanding against the shared layer.

    Clearing the cache means each test constructs its own layer on its own loop,
    so there is nothing to contend over. The discarded layer's connections
    belonged to a closed loop and cannot be awaited shut from synchronous
    fixture teardown, so they are dropped for garbage collection rather than
    closed politely.

    This is the same class of defect as `clear_driver_redis_state` in
    tests/conftest.py: shared process state that pytest-django's database
    rollback does not reach. Order-dependent failures that look like product
    defects are the worst kind of CI signal, so both are fixed rather than
    worked around by running one file at a time.
    """
    from channels import layers

    layers.channel_layers.backends.clear()
    try:
        yield
    finally:
        layers.channel_layers.backends.clear()
