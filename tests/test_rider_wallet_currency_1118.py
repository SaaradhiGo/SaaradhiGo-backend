"""18 Nov (QA, USA): a US rider's wallet showed rupees because the balance view opened it as INR."""
from decimal import Decimal

import pytest

from servers.rider.models import Wallet, WalletTransaction
from django.contrib.auth import get_user_model
from servers.rider.models import Rider
from tests.finance_world import client_for, make_rider


def us_rider(n):
    phone = f'+1208555{n:04d}'  # a Boise number
    user = get_user_model().objects.create_user(phone_number=phone, username=phone, full_name=f'Rider {n} Smith', role='rider')
    Rider.objects.create(user_id=user)
    return user

pytestmark = pytest.mark.django_db


def test_a_us_rider_sees_dollars_and_an_unused_inr_wallet_is_corrected():
    rider = us_rider(1)
    data = client_for(rider).get('/api/v1/rider/wallet/balance/').json()['data']
    assert data['currency'] == 'USD'

    other = us_rider(2)
    Wallet.objects.create(user_id=other, scope=Wallet.SCOPE_RIDER, balance=Decimal('0'), currency='INR')
    data = client_for(other).get('/api/v1/rider/wallet/balance/').json()['data']
    assert data['currency'] == 'USD'
    assert Wallet.objects.get(user_id=other, scope=Wallet.SCOPE_RIDER).currency == 'USD'


def test_a_wallet_that_has_moved_keeps_its_currency():
    rider = us_rider(3)
    Wallet.objects.create(user_id=rider, scope=Wallet.SCOPE_RIDER, balance=Decimal('0'), currency='INR')
    WalletTransaction.objects.create(user_id=rider, amount_minor=500, currency='INR', txn_type='credit', status='completed')
    client_for(rider).get('/api/v1/rider/wallet/balance/')
    assert Wallet.objects.get(user_id=rider, scope=Wallet.SCOPE_RIDER).currency == 'INR'


def test_an_indian_rider_stays_in_rupees():
    rider = make_rider('I1', '91')
    assert client_for(rider).get('/api/v1/rider/wallet/balance/').json()['data']['currency'] == 'INR'
