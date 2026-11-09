"""Payout settings per state and city, the payout cycle and the instant fee (owner, 2026-11-09).

THE RULING
----------
* By default drivers are paid automatically on a cycle, free to the driver.
* Early withdrawal ("instant") is optional per market. The driver pays ONLY the provider's own
  fee, passed through exactly; SaaradhiGo takes nothing (`fee_percent` stays 0).

RESOLUTION
----------
A driver's settings are their home city's override, then their state's, then the country row
(`PayoutCountryConfig`). Each FIELD falls back on its own: a city that sets only the payout time
keeps its state's (or country's) schedule. `EffectivePayoutConfig` reads like the country row
(every caller of `config_for_driver` keeps working) and says where each value came from.

THE CYCLE
---------
`weekly`: on `payout_weekday`, keyed by the Monday of the week (unchanged).
`every_n_days`: every `payout_interval_days` days from `payout_anchor_date`, keyed by that date.
Both run from `payout_time_local` in the country's zone. A state or city that sets any schedule
field runs on its own (`schedule_scope`), so `PayoutRun` is one per (country, scope, cycle).
"""

from datetime import datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from django.utils import timezone

from base.countries import get_country

SCHEDULE_FIELDS = ('payout_schedule', 'payout_weekday', 'payout_interval_days',
                   'payout_anchor_date', 'payout_time_local')
SCHEDULED = ('weekly', 'every_n_days')
ARRIVAL_TEXT = {'razorpayx': 'Usually within 30 minutes', 'razorpay': 'Usually within 30 minutes',
                'stripe_connect': 'Usually within 30 minutes'}


def _unset(value):
    return value is None or value == ''


class EffectivePayoutConfig:
    """The country row with a state's and a city's overrides on top. Reads like the row."""

    def __init__(self, base, overrides=()):
        from .models import OVERRIDE_FIELDS

        self._base = base
        self.overrides = list(overrides)          # most specific first
        self.sources = {}
        for name in OVERRIDE_FIELDS:
            value, source = getattr(base, name), 'country'
            for row in self.overrides:
                if not _unset(getattr(row, name)):
                    value, source = getattr(row, name), row.scope_code
                    break
            setattr(self, name, value)
            self.sources[name] = source
        scoped = [self.sources[n] for n in SCHEDULE_FIELDS if self.sources[n] != 'country']
        # The most specific level that sets any schedule field runs its own cycle.
        self.schedule_scope = ''
        for row in self.overrides:
            if row.scope_code in scoped:
                self.schedule_scope = row.scope_code
                break

    def __getattr__(self, name):
        return getattr(self._base, name)

    @property
    def base(self):
        return self._base

    def as_dict(self):
        out = dict(self._base.as_dict())
        for name in self.sources:
            value = getattr(self, name)
            if name == 'payout_time_local' and value is not None:
                value = value.strftime('%H:%M')
            elif name == 'instant_fee_percent' and value is not None:
                value = str(value)
            out[name] = value
        out['sources'] = dict(self.sources)
        out['schedule_scope'] = self.schedule_scope
        return out


def _overrides_for(codes):
    from .models import PayoutScopeOverride

    rows = {r.scope_code: r for r in PayoutScopeOverride.objects.filter(
        scope_code__in=[c for c in codes if c], is_active=True)}
    return [rows[c] for c in codes if c in rows]


def scope_codes_of_node(node):
    """`[city_code, state_code]` (most specific first) for a city or state GeoNode."""
    out = []
    while node is not None and node.kind in ('city', 'state'):
        out.append(node.code)
        node = node.parent
    return out


def effective_config(country, scope_codes=()):
    """The settings for a country and (optionally) its state/city codes, or None."""
    from .payout_config import active_config

    base = active_config(country)
    if base is None:
        return None
    return EffectivePayoutConfig(base, _overrides_for(list(scope_codes)))


def effective_for_driver(driver):
    """`(country, EffectivePayoutConfig or None)` for the driver's home city."""
    from .payout_config import driver_payout_country

    country = driver_payout_country(driver)
    home = getattr(driver, 'home_city', None)
    codes = scope_codes_of_node(home) if home is not None else []
    if home is not None and home.country is not None and home.country.code != country:
        codes = []
    return country, effective_config(country, codes)


# ---------------------------------------------------------------------------
# The instant fee: one source for what is shown and what is charged
# ---------------------------------------------------------------------------

def instant_fee(config, amount_minor):
    """The provider's fee for an instant withdrawal of `amount_minor`, in minor units.

    percent of the amount (rounded half up) plus the band fee (`instant_fee_tiers`) plus the
    fixed part, then held between the minimum and the maximum. THE ONLY SOURCE: the confirmation shows it and the withdrawal charges it.
    """
    if config is None or not amount_minor:
        return 0
    pct = Decimal(str(config.instant_fee_percent or 0))
    fee = int((Decimal(int(amount_minor)) * pct / Decimal('100'))
              .quantize(Decimal('1'), rounding=ROUND_HALF_UP))
    fee += tier_fee(getattr(config, 'instant_fee_tiers', None), amount_minor)
    fee += int(config.instant_cashout_fee_flat_minor or 0)
    if config.instant_fee_min_minor is not None:
        fee = max(fee, int(config.instant_fee_min_minor))
    if config.instant_fee_max_minor is not None:
        fee = min(fee, int(config.instant_fee_max_minor))
    return max(fee, 0)


def tier_fee(tiers, amount_minor):
    """The band fee for `amount_minor` from `[[up_to_minor or null, fee_minor], ...]`, or 0."""
    for up_to, fee in tiers or ():
        if up_to is None or int(amount_minor) <= int(up_to):
            return int(fee)
    return 0


def provider_label(config):
    name = (getattr(config, 'instant_fee_provider', '') or '').strip()
    return f'Provider fee (charged by {name})' if name else 'Provider fee'


# ---------------------------------------------------------------------------
# The cycle
# ---------------------------------------------------------------------------

def zone_of(config):
    return ZoneInfo(get_country(config.country).default_timezone)


def cycle_start(config, local_date):
    """The cycle that pays on `local_date`, or None when it is not a payout day."""
    if config.payout_schedule == 'weekly':
        if config.payout_weekday is None or local_date.weekday() != config.payout_weekday:
            return None
        return local_date - timedelta(days=local_date.weekday())
    if config.payout_schedule == 'every_n_days':
        n, anchor = config.payout_interval_days, config.payout_anchor_date
        if not n or anchor is None or local_date < anchor:
            return None
        return local_date if (local_date - anchor).days % n == 0 else None
    return None


def due_cycle(config, now=None):
    """The cycle to run NOW (a date), or None: the payout day, at or after the payout time."""
    now = now or timezone.now()
    local = now.astimezone(zone_of(config))
    start = cycle_start(config, local.date())
    if start is None:
        return None
    if config.payout_time_local is not None and local.time() < config.payout_time_local:
        return None
    return start


def next_payout_at(config, now=None):
    """The next scheduled payout as an aware datetime in the country's zone, or None."""
    if config is None or config.payout_schedule not in SCHEDULED:
        return None
    now = now or timezone.now()
    tz = zone_of(config)
    local = now.astimezone(tz)
    at = config.payout_time_local or time(0, 0)
    for offset in range(0, 400):
        day = local.date() + timedelta(days=offset)
        if cycle_start(config, day) is None:
            continue
        when = datetime.combine(day, at, tzinfo=tz)
        if when >= local:
            return when
    return None


# ---------------------------------------------------------------------------
# What the driver sees
# ---------------------------------------------------------------------------

def next_payout(driver, config, available_minor, now=None):
    """"Next payout: {date} - estimated {amount}", or None when the market pays on request.
    The estimate is today's balance (what a run now would pay), 0 below the minimum."""
    from base.money import from_minor

    when = next_payout_at(config, now)
    if when is None:
        return None
    estimate = available_minor if available_minor >= max(1, config.minimum_withdrawal_minor) else 0
    return {'date': when.date().isoformat(), 'at': when.isoformat(),
            'estimated_amount': str(from_minor(estimate, config.currency)),
            'estimated_amount_minor': estimate, 'currency': config.currency,
            'below_minimum': estimate == 0 and available_minor > 0}


def instant_quote(driver, config, amount_minor):
    """The withdraw-now confirmation: amount, provider fee, "You receive", bank, arrival.
    The fee is `instant_fee`, the same function the withdrawal charges."""
    from base.money import from_minor

    from .payout_destination import destination_label

    fee = instant_fee(config, amount_minor)
    receive = amount_minor - fee
    provider = (config.instant_fee_provider or '').strip()
    cur = config.currency
    return {'amount': str(from_minor(amount_minor, cur)), 'amount_minor': amount_minor,
            'provider_fee': str(from_minor(fee, cur)), 'provider_fee_minor': fee,
            'provider_fee_label': provider_label(config),
            'saaradhigo_fee': str(from_minor(0, cur)), 'saaradhigo_fee_minor': 0,
            'you_receive': str(from_minor(max(receive, 0), cur)),
            'you_receive_minor': max(receive, 0), 'currency': cur,
            'destination': destination_label(driver),
            'arrival': ARRIVAL_TEXT.get(provider.lower().replace(' ', ''),
                                        'Usually within 30 minutes'),
            'valid': receive > 0}
