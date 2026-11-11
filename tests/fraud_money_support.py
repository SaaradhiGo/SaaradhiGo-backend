"""Builders for the fraud money action tests (AUDIT 27.56).

Low-entropy fixture values only (the secret scan reads every commit). No network. Built on the
module 13 world (Hyderabad and Austin, every role) and the Finance world (drivers with wallets).
"""

from datetime import timedelta
from io import StringIO

from django.core.management import call_command
from django.utils import timezone

from conftest import seed_actor
from finance_world import make_driver
from fraud_support import (  # noqa: F401
    ADM, block_the_network, body_of, client_for, error_code, flag_url, geo, get, hyd_driver,
    make_flag, make_rider, operator, post, seed_rules, world,
)
from servers.driver.models import WithdrawalRequest
from servers.promotions.models import (
    INCENTIVE_REWARD, REFERRAL_REWARD, Campaign, LedgerEntry, Referral,
)
from servers.rider.models import Wallet, WalletTransaction, get_wallet

APPROVALS = '/api/v1/approvals/admin/requests'


def seed_money():
    """Approval policies (India and US) and the payout rules of both countries."""
    call_command('seed_approval_policies', actor_email=seed_actor().email, stdout=StringIO())
    call_command('apply_payout_ruling', actor_email=seed_actor().email, stdout=StringIO())


def us_driver(world, tag='U1'):
    return make_driver(world.geo, 'US-TX-AUS', tag, currency='USD', calling='1')


def campaign(world, *, kind='driver_incentive', node='IN-TG-HYD', country='IN', currency='INR',
             name='Weekend 3'):
    return Campaign.objects.create(
        kind=kind, name=name, state='live', country=country, geo_node=world.geo[node],
        currency=currency, budget_minor=10_000_000,
        config={'target_trips': 3, 'reward_minor': 30_000, 'audience': 'rider',
                'required_trips': 2, 'referrer_reward_minor': 10_000,
                'referee_reward_minor': 5_000},
        starts_at=timezone.now() - timedelta(days=1), ends_at=timezone.now() + timedelta(days=30))


def credit(user, minor, *, purpose='trip_earnings', scope=Wallet.SCOPE_DRIVER, currency='INR',
           at=None, key=None):
    """A completed wallet credit (trip earnings by default), the wallet balance moved with it."""
    wallet = get_wallet(user, scope, lock=True)
    if wallet.currency != currency:
        wallet.currency = currency
    txn = WalletTransaction.objects.create(
        user_id=user, currency=currency, amount_minor=minor, txn_type='credit',
        status='completed', purpose=purpose, reference_id=f'T_{purpose}',
        idempotency_key=key or f'{purpose}-{user.pk}-{minor}-{WalletTransaction.objects.count()}')
    wallet.balance_minor += minor
    wallet.save()
    if at is not None:
        WalletTransaction.objects.filter(pk=txn.pk).update(created_at=at)
    return txn


def reward(user, camp, minor, *, kind=INCENTIVE_REWARD, scope=Wallet.SCOPE_DRIVER, key=None,
           at=None):
    """An incentive or referral reward exactly as the promotions code writes it: a wallet credit
    plus the campaign ledger line that names it."""
    txn = credit(user, minor, purpose=kind, scope=scope, currency=camp.currency, at=at,
                 key=(key or f'{kind}:{camp.pk}:{user.pk}') + ':wt')
    entry = LedgerEntry.objects.create(
        kind=kind, campaign=camp, user=user, amount_minor=minor, currency=camp.currency,
        wallet_transaction=txn, idempotency_key=key or f'{kind}:{camp.pk}:{user.pk}')
    if at is not None:
        LedgerEntry.objects.filter(pk=entry.pk).update(created_at=at)
        entry.refresh_from_db()
    return entry


def referral_reward(referrer, referee, camp, minor, *, scope=Wallet.SCOPE_RIDER):
    ref = Referral.objects.create(campaign=camp, referrer=referrer, referee=referee,
                                  code_used='ABC123', status='rewarded', currency=camp.currency,
                                  referrer_reward_minor=minor)
    return reward(referrer, camp, minor, kind=REFERRAL_REWARD, scope=scope,
                  key=f'referral:{ref.pk}:referrer'), ref


def paid_out(driver, minor, *, at=None, status='completed'):
    """A payout that took money after `at`: any non-rejected withdrawal made later counts a reward
    credited before it as paid."""
    w = WithdrawalRequest.objects.create(driver=driver, amount_minor=minor, status=status)
    wallet = get_wallet(driver.user_id, Wallet.SCOPE_DRIVER, lock=True)
    wallet.balance_minor = max(0, wallet.balance_minor - minor)      # the money left the wallet
    wallet.save()
    if at is not None:
        WithdrawalRequest.objects.filter(pk=w.pk).update(requested_at=at)
    return w


def farming_flag(world, user, camp, *, status='open', node='IN-TG-HYD'):
    return make_flag(user, world.geo[node], kind='incentive_farming', status=status,
                     evidence={'signals': ['short_trips'], 'refs': {'campaign_ids': [camp.pk]},
                               'measures': {}, 'hit_count': 1})


def decide(client, flag, decision, reason, note='Reviewed the activity'):
    return post(client, flag_url(flag, f'{decision}/'), {'reason_code': reason, 'note': note})


def approve(request_id):
    from test_finance_actions import approve_request, checker

    chk, _ = checker()
    return approve_request(chk, request_id)
