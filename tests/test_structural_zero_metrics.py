"""Financial dashboards must not present figures that cannot be non-zero.

Two numbers were being shown as data while being structurally incapable of ever
being anything but zero:

* `cancellation_fees` — queried TransactionHistory for `method__icontains='cancel'`,
  while every method ever written is wallet / online / cash / deferred. Empty by
  construction. On a revenue dashboard a permanent ₹0 reads as "we charge no
  cancellation fees", which is a business statement nobody has made.
* `total_redemptions` — counts PromoRedemption, and `promos.redeem_promo` has no
  callers. It sat beside `active_promos`, which is real, so "0 redemptions /
  3 active promos" reads as a campaign nobody used rather than a feature that is
  not wired.

These tests pin both, and pin that the database column was kept.
"""

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from servers.ride.models import Trip

User = get_user_model()


@pytest.fixture
def admin_client(db, client):
    """A session-authenticated staff admin, as the dashboards require."""
    admin = User.objects.create_user(phone_number='+919700000901', role='admin')
    admin.is_staff = True
    admin.set_password('qa-not-a-real-password')
    admin.save()
    assert client.login(username='+919700000901', password='qa-not-a-real-password')
    return client


# ---------------------------------------------------------------------------
# cancellation_fees: removed
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_the_transaction_dashboard_reports_no_cancellation_fee_total(admin_client):
    """The context key is gone, so no template can render a phantom revenue line."""
    resp = admin_client.get(reverse('transaction_dashboard'))
    assert resp.status_code == 200
    assert 'cancellation_fees' not in resp.context


@pytest.mark.django_db
def test_cancellation_fee_is_not_offered_as_a_transaction_type(admin_client):
    """A filter option that can never match anything is worse than no option."""
    resp = admin_client.get(reverse('transaction_dashboard'))
    types = dict(resp.context['transaction_types'])
    assert 'cancellation_fee' not in types
    # The genuine types must survive.
    for kept in ('rider_payment', 'driver_earnings', 'refund'):
        assert kept in types


@pytest.mark.django_db
def test_no_transaction_row_claims_to_be_a_cancellation_fee(admin_client):
    resp = admin_client.get(reverse('transaction_dashboard'))
    rows = resp.context['transactions']
    assert all(r['type'] != 'cancellation_fee' for r in rows)


@pytest.mark.django_db
def test_the_cancellation_fee_column_is_deliberately_kept():
    """Removing the metric must not remove the destination for the real value.

    When a cancellation policy exists, this is where the amount goes.
    """
    assert any(f.name == 'cancellation_fee' for f in Trip._meta.get_fields())


# ---------------------------------------------------------------------------
# total_redemptions: explicit rather than misleading
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_the_promo_page_declares_redemption_unwired(admin_client):
    """An explicit state, not a zero that invites the wrong conclusion."""
    resp = admin_client.get(reverse('promo_codes'))
    assert resp.status_code == 200
    assert resp.context['redemptions_enabled'] is False
    content = resp.content.decode()
    assert 'Not enabled yet' in content
    # And it says why, on the page, where whoever runs a campaign will see it.
    assert 'redemption is not wired into booking' in content


@pytest.mark.django_db
def test_the_promo_page_still_reports_the_counts_that_are_real(admin_client):
    """Codes really can be created, so those numbers are not touched."""
    resp = admin_client.get(reverse('promo_codes'))
    assert 'total_promos' in resp.context
    assert 'active_promos' in resp.context


@pytest.mark.django_db
def test_no_redemption_data_is_fabricated(admin_client):
    """The count is still the true count; only its presentation changed."""
    from servers.ride.models import PromoRedemption

    resp = admin_client.get(reverse('promo_codes'))
    assert resp.context['total_redemptions'] == PromoRedemption.objects.count() == 0


# ---------------------------------------------------------------------------
# Nothing financial moved
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_the_cleanup_is_presentation_only(admin_client):
    """Both pages are reads. No row anywhere may change."""
    from servers.payments.models import TransactionHistory
    from servers.rider.models import WalletTransaction

    before = (
        list(TransactionHistory.objects.values()),
        list(WalletTransaction.objects.values()),
        list(Trip.objects.values('id', 'estimated_fare', 'final_fare',
                                 'cancellation_fee')),
    )

    admin_client.get(reverse('transaction_dashboard'))
    admin_client.get(reverse('promo_codes'))

    after = (
        list(TransactionHistory.objects.values()),
        list(WalletTransaction.objects.values()),
        list(Trip.objects.values('id', 'estimated_fare', 'final_fare',
                                 'cancellation_fee')),
    )
    assert after == before
