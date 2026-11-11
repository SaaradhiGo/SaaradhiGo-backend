"""The Finance and reconciliation query shapes are answered from indexes.

PostgreSQL only (`pytest -m postgres`). With sequential scans switched off the
planner takes an index whenever one can serve the filter; a `Seq Scan` that
remains is a table with no usable index for the shape, which on a production
sized table is a full scan per request or per nightly run. The tables here are
the big ones: payments, trips, settlements, withdrawals, webhook events and the
transaction history.
"""

from datetime import timedelta

import pytest
from django.db import connection
from django.db.models import Exists, OuterRef
from django.utils import timezone

pytestmark = [pytest.mark.django_db, pytest.mark.postgres]


def plan_of(queryset):
    sql, params = queryset.query.sql_with_params()
    with connection.cursor() as cursor:
        cursor.execute('SET enable_seqscan = off')
        try:
            cursor.execute('EXPLAIN ' + sql, params)
            return ' '.join(row[0] for row in cursor.fetchall())
        finally:
            cursor.execute('RESET enable_seqscan')


def assert_indexed(queryset, table):
    plan = plan_of(queryset)
    assert f'Seq Scan on {table}' not in plan, plan


NOW = timezone.now()
DAY = (NOW - timedelta(days=1), NOW)


def test_the_daily_payment_window_uses_the_created_at_index():
    from servers.payments.models import Payment

    qs = Payment.objects.filter(
        created_at__gte=DAY[0], created_at__lt=DAY[1], payment_gateway='cashfree',
        method='online', status='completed', currency='INR', trip_id__zone__country='IN',
    ).select_related('trip_id__zone__geo_node')[:2000]
    assert_indexed(qs, 'payments_payment')


def test_the_webhook_lookups_use_the_gateway_received_index():
    from servers.payments.models import WebhookEvent

    qs = WebhookEvent.objects.filter(
        gateway='cashfree', received_at__gte=DAY[0], received_at__lt=DAY[1],
        event_type__in=('PAYMENT_SUCCESS_WEBHOOK', 'PAYMENT_SUCCESS'))
    assert_indexed(qs, 'payments_webhookevent')


def test_completed_trips_without_a_settlement_use_the_completed_at_index():
    from servers.payments.models import TripSettlement
    from servers.ride.models import Trip

    settled = TripSettlement.objects.filter(trip=OuterRef('pk'))
    qs = (Trip.objects.filter(completed_at__gte=DAY[0], completed_at__lt=DAY[1],
                              status_id__status_code='completed', driver_id__isnull=False,
                              currency='INR', zone__country='IN')
          .exclude(Exists(settled)))
    assert_indexed(qs, 'ride_trip')


def test_the_settlement_window_uses_the_settled_at_index():
    from django.db.models import F

    from servers.payments.models import TripSettlement

    qs = (TripSettlement.objects.filter(settled_at__gte=DAY[0], settled_at__lt=DAY[1], currency='INR')
          .exclude(gross_fare_minor=F('trip__final_fare_minor')))
    assert_indexed(qs, 'payments_tripsettlement')


def test_the_stuck_payout_check_uses_the_status_processed_at_index():
    from servers.driver.models import WithdrawalRequest

    qs = WithdrawalRequest.objects.filter(
        status='processed', currency='INR', processed_at__lt=NOW - timedelta(hours=24),
        processed_at__gte=NOW - timedelta(days=30))
    assert_indexed(qs, 'driver_withdrawalrequest')


def test_the_payout_queue_date_filter_uses_the_requested_at_index():
    from servers.driver.models import WithdrawalRequest

    qs = WithdrawalRequest.objects.filter(
        requested_at__gte=DAY[0], requested_at__lt=DAY[1]).order_by('-requested_at')[:25]
    assert_indexed(qs, 'driver_withdrawalrequest')


def test_the_payments_paid_per_day_uses_the_created_at_index():
    from servers.payments.models import TransactionHistory

    qs = TransactionHistory.objects.filter(
        withdrawal_request__isnull=False, status='completed',
        created_at__gte=DAY[0], created_at__lt=DAY[1])
    assert_indexed(qs, 'payments_transactionhistory')


def test_the_open_findings_list_uses_the_open_index():
    from servers.payments.models import ReconciliationItem

    qs = ReconciliationItem.objects.filter(resolved_at__isnull=True).order_by('-created_at')[:25]
    assert_indexed(qs, 'payments_reconciliationitem')


def test_the_probe_can_fail():
    """A check that has never failed has never been shown to work: a filter
    on a column with no index is reported as a sequential scan."""
    from servers.driver.models import WithdrawalRequest

    plan = plan_of(WithdrawalRequest.objects.order_by().filter(failure_reason='nothing indexes this'))
    assert 'Seq Scan on driver_withdrawalrequest' in plan, plan
