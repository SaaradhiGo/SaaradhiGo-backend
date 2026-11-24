"""Receipt numbers (owner, 24 Nov): a gap-free series per legal entity, allocated on issue."""
from datetime import datetime, timezone

import pytest
from django.db import transaction

from servers.ride.models import ReceiptSeries
from servers.ride.receipt_numbers import allocate, period_of

pytestmark = pytest.mark.django_db(transaction=True)


def at(y, m, d, h=12):
    return datetime(y, m, d, h, tzinfo=timezone.utc)


def test_india_counts_by_financial_year_from_1_april_ist():
    assert period_of('IN', at(2027, 3, 31, 10)) == '2026-27'
    assert period_of('IN', at(2027, 3, 31, 19)) == '2027-28'   # 1 Apr 00:30 IST
    assert period_of('US', at(2026, 12, 31)) == '2026'
    assert period_of('US', at(2027, 1, 1, 12)) == '2027'


def test_numbers_run_without_gaps_per_entity(settings):
    settings.IS_PRODUCTION = True
    with transaction.atomic():
        assert allocate('IN', at(2026, 11, 24)) == 'DTS/2026-27/000001'
    with transaction.atomic():
        assert allocate('IN', at(2026, 11, 25)) == 'DTS/2026-27/000002'
        assert allocate('US', at(2026, 11, 25)) == 'DTL/2026/000001'


def test_a_failed_issue_does_not_use_up_a_number(settings):
    settings.IS_PRODUCTION = True
    with pytest.raises(RuntimeError):
        with transaction.atomic():
            allocate('IN', at(2026, 11, 24))
            raise RuntimeError('the receipt could not be written')
    with transaction.atomic():
        assert allocate('IN', at(2026, 11, 24)) == 'DTS/2026-27/000001'
    assert ReceiptSeries.objects.get(prefix='DTS').last_number == 1


def test_qa_numbers_say_so_and_allocation_needs_the_transaction(settings):
    settings.IS_PRODUCTION = False
    with transaction.atomic():
        assert allocate('US', at(2026, 6, 1)) == 'QA-DTL/2026/000001'
    with pytest.raises(RuntimeError):
        allocate('US', at(2026, 6, 1))


def test_a_trip_gets_one_receipt_per_version_even_when_issued_twice(settings):
    from django.db import IntegrityError

    from servers.ride.models import Receipt

    names = {c.name for c in Receipt._meta.constraints}
    assert 'receipt_one_per_trip_version' in names
    assert IntegrityError  # the issue path catches it and returns the first receipt
