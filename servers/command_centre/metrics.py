"""The metrics a Command Centre threshold can watch, and their defaults.

WHY A CLOSED CATALOGUE
----------------------
A threshold row names its metric by a string. If any string were accepted,
a typo in a seed or a request (`acceptence_rate`) would store a row the
sweep never evaluates, and the city would look covered while nothing
watched it. So the keys are closed, like `approvals.actions.ACTIONS` and
`admin_access.roles.ALL_PERMISSIONS`: an unknown key is a defect at the
boundary, not data.

HOW EACH ONE IS MEASURED (stated, because the brief asked)
----------------------------------------------------------
    unfulfilled_rate     trips requested in the window that were cancelled
                         before any driver accepted (`accepted_at IS NULL`
                         and status `cancelled`: the rider gave up or the
                         accept-deadline sweep timed out) ÷ trips requested
                         in the window. A trip still searching is not yet
                         unfulfilled and is not counted either way.
    acceptance_rate      trips accepted ÷ trips requested, in the window.
                         Offers are NOT persisted -- `redis_client.
                         add_offered_drivers` keeps them in Redis for the
                         life of the dispatch wave only -- so "accepted ÷
                         offers made" cannot be computed from the record.
                         The denominator is therefore REQUESTS, and the
                         response says so (`definitions`).
    avg_eta_minutes      mean of (`Trip.reached_at` − `Trip.accepted_at`)
                         over trips requested in the window that have both
                         stamps: the time a rider waited between a driver
                         saying yes and the car arriving.
    sos_open             SOS events anchored to the city with status
                         `open` (not yet acknowledged by anyone), now.
    sos_unacknowledged   age in minutes of the OLDEST open SOS in the
                         city, now. 0 when none is open.
    withdrawal_pending   age in hours of the OLDEST `pending` withdrawal
                         owed to one of the city's drivers, now. 0 when
                         none is pending.

The rate metrics are evaluated over `AlertThreshold.window` (`today`,
`24h` or `7d`); the three "now" metrics ignore it. A rate with fewer than
`min_sample` requests in its window is reported (so the dashboard shows
it) but never alerted on: 1 unfulfilled request out of 2 is not a 50 %
emergency, it is a quiet morning.

DEFAULTS (brief, Command Centre module; seeded by `seed_alert_thresholds`)
--------------------------------------------------------------------------
    unfulfilled rate       warn 15 %   critical 30 %
    acceptance rate        warn < 70 %  critical < 50 %
    average ETA            warn > 8 min critical > 12 min
    SOS open               critical > 0
    SOS unacknowledged     critical > 5 min
    pending withdrawals    warn > 48 h
"""

from dataclasses import dataclass
from decimal import Decimal

ABOVE, BELOW = 'above', 'below'
PERCENT, MINUTES, HOURS, COUNT = 'percent', 'minutes', 'hours', 'count'

WINDOW_TODAY, WINDOW_24H, WINDOW_7D, WINDOW_NOW = 'today', '24h', '7d', 'now'
#: Windows the overview endpoint accepts.
OVERVIEW_WINDOWS = (WINDOW_TODAY, WINDOW_24H, WINDOW_7D)
#: Windows a rate threshold may be evaluated over.
RATE_WINDOWS = OVERVIEW_WINDOWS


@dataclass(frozen=True)
class Metric:
    key: str
    label: str
    unit: str
    #: `above`: the alert fires when the value EXCEEDS the threshold.
    #: `below`: when it falls UNDER it.
    direction: str
    #: Seed values; None means "no level of this severity".
    default_warn: Decimal | None
    default_critical: Decimal | None
    #: The window the seed evaluates over; `now` for stock metrics.
    default_window: str
    #: Smallest denominator before a RATE may raise an alert.
    min_sample: int = 0
    #: Which team acknowledges the alert this metric raises (ACK_PERMISSION).
    ack_permission: str = 'trip.write'

    @property
    def is_rate(self):
        return self.default_window != WINDOW_NOW

    @property
    def alert_kind(self):
        return f'threshold_{self.key}'


UNFULFILLED_RATE = 'unfulfilled_rate'
ACCEPTANCE_RATE = 'acceptance_rate'
AVG_ETA_MINUTES = 'avg_eta_minutes'
SOS_OPEN = 'sos_open'
SOS_UNACKNOWLEDGED = 'sos_unacknowledged'
WITHDRAWAL_PENDING = 'withdrawal_pending'

METRICS = {
    m.key: m for m in (
        Metric(UNFULFILLED_RATE, 'Unfulfilled-request rate', PERCENT, ABOVE,
               Decimal('15'), Decimal('30'), WINDOW_24H, min_sample=5),
        Metric(ACCEPTANCE_RATE, 'Acceptance rate', PERCENT, BELOW,
               Decimal('70'), Decimal('50'), WINDOW_24H, min_sample=5),
        Metric(AVG_ETA_MINUTES, 'Average ETA (accept to arrive)', MINUTES, ABOVE,
               Decimal('8'), Decimal('12'), WINDOW_24H, min_sample=5),
        Metric(SOS_OPEN, 'Open SOS', COUNT, ABOVE,
               None, Decimal('0'), WINDOW_NOW, ack_permission='safety.respond'),
        Metric(SOS_UNACKNOWLEDGED, 'Oldest unacknowledged SOS', MINUTES, ABOVE,
               None, Decimal('5'), WINDOW_NOW, ack_permission='safety.respond'),
        Metric(WITHDRAWAL_PENDING, 'Oldest pending withdrawal', HOURS, ABOVE,
               Decimal('48'), None, WINDOW_NOW, ack_permission='finance.read'),
    )
}

ALL_METRIC_KEYS = frozenset(METRICS)


def metric_for(key):
    """The catalogue entry, or raise: an unknown key is a caller defect."""
    try:
        return METRICS[key]
    except KeyError:
        raise ValueError(f'Unknown Command Centre metric {key!r}') from None


def level_for(metric, value, warn, critical):
    """`'critical'`, `'warning'` or None for `value` against the two lines.

    A None line means that severity does not exist for this threshold
    (SOS has no warning level; pending withdrawals have no critical one).
    A None value (nothing to measure) never alerts.
    """
    if value is None:
        return None
    value = Decimal(str(value))

    def crossed(line):
        if line is None:
            return False
        line = Decimal(str(line))
        return value > line if metric.direction == ABOVE else value < line

    if crossed(critical):
        return 'critical'
    if crossed(warn):
        return 'warning'
    return None


def describe(metric):
    """The catalogue entry as the API shows it."""
    return {
        'key': metric.key,
        'label': metric.label,
        'unit': metric.unit,
        'direction': metric.direction,
        'default_warn': _num(metric.default_warn),
        'default_critical': _num(metric.default_critical),
        'default_window': metric.default_window,
        'min_sample': metric.min_sample,
        'ack_permission': metric.ack_permission,
        'alert_kind': metric.alert_kind,
    }


def _num(value):
    """A Decimal as JSON text: '15', '7.5', never '1.5E+1'."""
    if value is None:
        return None
    d = Decimal(str(value))
    return str(d.quantize(Decimal(1))) if d == d.to_integral() else str(d.normalize())


#: Plain-language definitions, sent with every overview so a reader of the
#: dashboard does not have to open this file to know what a rate divides.
DEFINITIONS = {
    'unfulfilled_rate': (
        'Requests in the window cancelled before any driver accepted '
        '(accepted_at is null and status is cancelled) ÷ requests in the window.'
    ),
    'acceptance_rate': (
        'Trips accepted ÷ trips requested, in the window. Offers are not '
        'persisted (they live in Redis for one dispatch wave), so the '
        'denominator is requests, not offers.'
    ),
    'avg_eta_minutes': (
        'Mean of reached_at − accepted_at over trips requested in the window '
        'that carry both timestamps.'
    ),
    'collected': (
        'Charged fare (final_fare_minor, else estimated_fare_minor) of trips '
        'completed in the window, per currency; never summed across currencies.'
    ),
    'drivers_online_now': (
        'Drivers of this city (home_city) with a live presence heartbeat in '
        'Redis; when Redis is unavailable, drivers whose status column is '
        '"online" (the response says which, in drivers.online_source).'
    ),
    'last_week': 'The same window shifted back exactly seven days.',
}
