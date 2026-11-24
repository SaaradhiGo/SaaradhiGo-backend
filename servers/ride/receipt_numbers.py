"""Receipt and tax-invoice numbers: a gap-free running series per legal entity (owner, 24 Nov).

    DeepTrics Software Solutions Pvt Ltd (India)  DTS/2026-27/000001  financial year from 1 April
    DeepTrics LLC (United States)                 DTL/2026/000001     calendar year from 1 January

The number is allocated when the receipt is issued, inside the same transaction that writes it: a
failure rolls the counter back with the receipt, so no number is ever skipped. Each environment
keeps its own series (its own database); outside production the number starts "QA-" so a test
receipt never looks like a real invoice. Receipts issued before this keep the numbers they have.
It is separate from the trip's reference (base.public_ref).
"""
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import transaction
from django.utils import timezone

#: country -> (series prefix, time zone the period is read in, financial year starts in April?)
SERIES = {
    'IN': ('DTS', 'Asia/Kolkata', True),
    'US': ('DTL', 'America/Boise', False),
}


def period_of(country, when):
    """'2026-27' for an Indian date from 1 Apr 2026 to 31 Mar 2027; '2026' for a US date."""
    _prefix, tz, april = SERIES.get(country, SERIES['IN'])
    day = when.astimezone(ZoneInfo(tz)).date()
    if not april:
        return str(day.year)
    start = day.year if day.month >= 4 else day.year - 1
    return f'{start}-{str(start + 1)[-2:]}'


def allocate(country, when=None):
    """The next number in this entity's series for `when`. Must run inside the caller's transaction
    (`transaction.atomic`), so the receipt and the counter commit or roll back together."""
    from servers.ride.models import ReceiptSeries

    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError('allocate() must run inside the transaction that writes the receipt')
    country = (country or 'IN').upper()
    prefix = SERIES.get(country, SERIES['IN'])[0]
    period = period_of(country, when or timezone.now())
    row = ReceiptSeries.objects.select_for_update().filter(prefix=prefix, period=period).first()
    if row is None:
        from django.db import IntegrityError
        try:
            with transaction.atomic():  # savepoint: a concurrent first issue may create it first
                ReceiptSeries.objects.create(prefix=prefix, period=period)
        except IntegrityError:
            pass
        row = ReceiptSeries.objects.select_for_update().get(prefix=prefix, period=period)
    row.last_number += 1
    row.save(update_fields=['last_number'])
    number = f'{prefix}/{period}/{row.last_number:06d}'
    return number if getattr(settings, 'IS_PRODUCTION', False) else f'QA-{number}'
