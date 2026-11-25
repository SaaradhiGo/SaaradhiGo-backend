"""References people see (owner, 24 Nov): never a sequential database id.

A reference says where a record belongs and cannot be guessed or counted:

    HYD-261116-K7Q4   a trip: its city, local day (YYMMDD), 4 random characters
    D-HYD-7K3Q        a driver (home city)      R-HYD-9MX2   a rider (home city)
    SUP-HYD-4KQ7      a support ticket          PO-HYD-M3XA  a payout
    APR-HYD-Q7KD      an approval request       FR-HYD-T9PC  a fraud flag
    INC-HYD-4MQ9      a safety incident (25 Nov: SOS or not; the kind says which)

The random part avoids letters people confuse (0/O, 1/I/L, 2/Z, 5/S, 8/B). Old references
("T-232") are still understood by search, so nothing anyone wrote down stops working.
Tax receipt numbers are a separate, gap-free series and never come from here.
"""

import re
import secrets

ALPHABET = 'ACDEFGHJKMNPQRTUVWXY34679'
RANDOM_LEN = 4

TRIP, DRIVER, RIDER, TICKET, PAYOUT, APPROVAL, FRAUD, INCIDENT = (
    'trip', 'driver', 'rider', 'ticket', 'payout', 'approval', 'fraud', 'incident')
PREFIX = {DRIVER: 'D', RIDER: 'R', TICKET: 'SUP', PAYOUT: 'PO', APPROVAL: 'APR', FRAUD: 'FR', INCIDENT: 'INC'}
KIND_OF_PREFIX = {v: k for k, v in PREFIX.items()}

#: A city's short code: the last part of its place code, except where people say it differently.
SHORT = {'US-ID-TV': 'TV'}

_NEW = re.compile(r'^\s*(?:(D|R|SUP|PO|APR|FR|INC)-)?([A-Z]{2,4})-(?:(\d{6})-)?([' + ALPHABET + r']{4})\s*$', re.IGNORECASE)


def city_short(place_code):
    """'HYD' for 'IN-TG-HYD', 'TV' for 'US-ID-TV'; the country code for a country; 'SG' for none."""
    if not place_code:
        return 'SG'
    code = place_code.upper()
    if code in SHORT:
        return SHORT[code]
    return code.split('-')[-1][:4]


def random_part():
    return ''.join(secrets.choice(ALPHABET) for _ in range(RANDOM_LEN))


def make(kind, place_code, day=None):
    """A fresh reference (not yet checked for uniqueness): see the module doc for the shapes."""
    city = city_short(place_code)
    if kind == TRIP:
        return f"{city}-{day.strftime('%y%m%d') if day else '000000'}-{random_part()}"
    return f'{PREFIX[kind]}-{city}-{random_part()}'


def assign(obj, kind, place_code, day=None, field='public_ref', attempts=8):
    """Give `obj` a unique reference and save just that field. Idempotent: keeps one it has."""
    if getattr(obj, field, None):
        return getattr(obj, field)
    model = type(obj)
    for _ in range(attempts):
        candidate = make(kind, place_code, day)
        if not model.objects.filter(**{field: candidate}).exists():
            model.objects.filter(pk=obj.pk, **{f'{field}__isnull': True}).update(**{field: candidate})
            setattr(obj, field, candidate)
            return candidate
    raise RuntimeError(f'Could not find a free {kind} reference for {place_code}')


def parse(text):
    """('trip', 'HYD-261116-K7Q4') for a new-style reference, else None (old ones: base.refs.parse)."""
    m = _NEW.match(text or '')
    if not m:
        return None
    prefix, _city, day, _rand = m.groups()
    if prefix:
        return KIND_OF_PREFIX[prefix.upper()], text.strip().upper()
    return (TRIP, text.strip().upper()) if day else None


# ---------------------------------------------------------------------------
# Where each record belongs, and assigning on create
# ---------------------------------------------------------------------------

def _code(node):
    return getattr(node, 'code', None)


def _trip_place(trip):
    zone = getattr(trip, 'zone', None)
    city = _code(getattr(zone, 'geo_node', None)) if zone else None
    day = None
    when = getattr(trip, 'requested_at', None)
    if when is not None:
        from zoneinfo import ZoneInfo
        try:
            day = when.astimezone(ZoneInfo(getattr(zone, 'timezone_name', '') or 'Asia/Kolkata')).date()
        except Exception:  # noqa: BLE001 - an unknown zone name falls back to the UTC day
            day = when.date()
    return city or getattr(zone, 'country', None) or getattr(trip, 'currency_country', None), day


def _user_city(user):
    for attr in ('driver', 'rider'):
        try:
            node = getattr(getattr(user, attr, None), 'home_city', None)
        except Exception:  # noqa: BLE001 - a missing reverse one-to-one raises
            node = None
        if node is not None:
            return node.code
    return None


def place_of(kind, obj):
    """(place code, local day or None) for a record of `kind`."""
    if kind == TRIP:
        return _trip_place(obj)
    if kind in (DRIVER, RIDER):
        return _code(getattr(obj, 'home_city', None)), None
    if kind == TICKET:
        trip = getattr(obj, 'trip_id', None)
        if trip is not None:
            return _trip_place(trip)[0], None
        return _user_city(getattr(obj, 'user_id', None)) or getattr(obj, 'country', None), None
    if kind == PAYOUT:
        return _code(getattr(getattr(obj, 'driver', None), 'home_city', None)), None
    if kind == APPROVAL:
        return _code(getattr(obj, 'scope_node', None)) or getattr(obj, 'country', None), None
    if kind == FRAUD:
        return _code(getattr(obj, 'anchor_node', None)), None
    if kind == INCIDENT:
        place = _code(getattr(obj, 'anchor_node', None))
        trip = getattr(obj, 'trip', None)
        if not place and trip is not None:
            place = _trip_place(trip)[0]
        return place or getattr(obj, 'country', None) or None, None
    return None, None


def give(kind, obj):
    """Assign `obj` its reference from where it belongs. Never raises: a record is saved either way,
    and `manage.py assign_public_refs` fills any gap."""
    try:
        place, day = place_of(kind, obj)
        return assign(obj, kind, place, day)
    except Exception:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).exception('public_ref_failed', extra={'kind': kind, 'pk': obj.pk})
        return None


#: (app label, model) -> kind, for the signal and the backfill.
MODELS = {
    ('ride', 'Trip'): TRIP, ('driver', 'Driver'): DRIVER, ('rider', 'Rider'): RIDER,
    ('support', 'SupportTicket'): TICKET, ('driver', 'WithdrawalRequest'): PAYOUT,
    ('approvals', 'ApprovalRequest'): APPROVAL, ('fraud', 'RiskFlag'): FRAUD,
    ('safety', 'SafetyIncident'): INCIDENT,
}


def connect():
    """Give every new record of the MODELS its reference after it is first saved."""
    from django.apps import apps
    from django.db.models.signals import post_save, pre_save

    def fresh_copy(sender, instance, **_kw):
        # A row saved as a copy (pk set to None) is a new record: it gets a reference of its own.
        if instance.pk is None and getattr(instance, 'public_ref', None):
            instance.public_ref = None

    for (app, name), kind in MODELS.items():
        pre_save.connect(fresh_copy, sender=apps.get_model(app, name), weak=False,
                         dispatch_uid=f'public_ref_copy:{app}.{name}')
        def handler(sender, instance, created, kind=kind, **_kw):
            if created and not instance.public_ref:
                give(kind, instance)
        post_save.connect(handler, sender=apps.get_model(app, name), weak=False,
                          dispatch_uid=f'public_ref:{app}.{name}')


def of(obj, fallback_prefix=None):
    """The reference to show for `obj`: its own, else (an old row not yet backfilled) the old form."""
    ref = getattr(obj, 'public_ref', None)
    if ref:
        return ref
    if obj is None or fallback_prefix is None:
        return None
    return f'{fallback_prefix}-{obj.pk}'
