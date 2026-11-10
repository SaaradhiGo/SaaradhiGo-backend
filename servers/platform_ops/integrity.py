"""Data integrity (10 Nov, after Srinu's account had a role and no Driver row).

Four checks, run once by `manage.py integrity_scan` and daily by Celery:
  * accounts with a role and no matching profile (driver, rider; an admin with no assignment)
  * drivers without a driver wallet
  * completed trips with a driver and no settlement row (`TRIP_<id>`), an hour after they ended
  * withdrawals (not rejected) without their ledger debit (`withdrawal_<id>`)

`fix=True` repairs what is safe to repair: the missing Driver or Rider row and the missing wallet
(empty, in the market's currency). Trips and payouts are never "fixed" here: money rows are
written by settlement and by the withdrawal itself, so a missing one is an alert for Finance.
Any finding raises one console alert (`platform.data_integrity`).
"""
import logging
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db.models import Exists, OuterRef
from django.utils import timezone

logger = logging.getLogger(__name__)
SAMPLE = 20
#: Display fixtures written completed and never settled on purpose (`seed_us_test_city`: a paused
#: city with no commission rule; trip 105 on QA). Not trips anyone drove, so not a missing ledger row.
FIXTURE_TRIP_KEYS = ('qa-us-seed-trip-completed',)


def scan(fix=False, now=None):
    from servers.admin_access.models import AdminAssignment
    from servers.driver.models import Driver, WithdrawalRequest
    from servers.ride.models import Trip
    from servers.rider.models import Wallet, WalletTransaction

    now = now or timezone.now()
    User = get_user_model()
    out = {}

    no_driver = list(User.objects.filter(role='driver', driver__isnull=True).values_list('id', flat=True))
    no_rider = list(User.objects.filter(role='rider', rider__isnull=True).values_list('id', flat=True))
    no_assignment = list(User.objects.filter(role='admin', is_active=True).exclude(
        Exists(AdminAssignment.objects.filter(admin=OuterRef('pk')))).values_list('id', flat=True))
    fixed_profiles = 0
    if fix and (no_driver or no_rider):
        from servers.auth_user.profiles import ensure_role_profile

        for user in User.objects.filter(id__in=no_driver + no_rider):
            if ensure_role_profile(user) is not None:
                fixed_profiles += 1
    out['role_without_profile'] = {'driver': no_driver[:SAMPLE], 'rider': no_rider[:SAMPLE],
                                   'admin_without_assignment': no_assignment[:SAMPLE],
                                   'found': len(no_driver) + len(no_rider) + len(no_assignment),
                                   'fixed': fixed_profiles}

    wallets = Wallet.objects.filter(user_id=OuterRef('user_id'), scope=Wallet.SCOPE_DRIVER)
    no_wallet = list(Driver.objects.filter(~Exists(wallets)).values_list('id', flat=True))
    fixed_wallets = 0
    if fix:
        from servers.driver.services import driver_currency

        for driver in Driver.objects.filter(id__in=no_wallet).select_related('user_id', 'home_city'):
            _, created = Wallet.objects.get_or_create(
                user_id=driver.user_id, scope=Wallet.SCOPE_DRIVER, defaults={'currency': driver_currency(driver)})
            fixed_wallets += int(created)
    out['driver_without_wallet'] = {'drivers': no_wallet[:SAMPLE], 'found': len(no_wallet), 'fixed': fixed_wallets}

    trips = Trip.objects.filter(status_id__status_code='completed', driver_id__isnull=False,
                                completed_at__lt=now - timedelta(hours=1)
                                ).exclude(client_request_id__in=FIXTURE_TRIP_KEYS)
    missing_trips = []
    for trip_id in trips.values_list('id', flat=True).iterator():
        ref = f'TRIP_{trip_id}'
        if not WalletTransaction.objects.filter(reference_id=ref).exists():
            missing_trips.append(trip_id)
    out['trip_without_ledger'] = {'trips': missing_trips[:SAMPLE], 'found': len(missing_trips), 'fixed': 0}

    from servers.payments.withdrawal_ledger import reference_of

    missing_payouts = [w.id for w in WithdrawalRequest.objects.exclude(status='rejected').only('id').iterator()
                       if not WalletTransaction.objects.filter(reference_id=reference_of(w.id)).exists()]
    out['payout_without_ledger'] = {'withdrawals': missing_payouts[:SAMPLE], 'found': len(missing_payouts),
                                    'fixed': 0}
    out['total_found'] = sum(v['found'] for v in out.values() if isinstance(v, dict))
    out['total_fixed'] = sum(v['fixed'] for v in out.values() if isinstance(v, dict))
    out['ran_at'] = now.isoformat()
    _alert(out)
    logger.info('integrity_scan found=%s fixed=%s', out['total_found'], out['total_fixed'])
    return out


def _alert(result):
    from servers.command_centre.models import DATA_INTEGRITY
    from servers.command_centre.services import raise_alert

    left = result['total_found'] - result['total_fixed']
    if left <= 0:
        return None
    parts = [f"{k.replace('_', ' ')}: {v['found'] - v['fixed']}" for k, v in result.items()
             if isinstance(v, dict) and v['found'] - v['fixed'] > 0]
    return raise_alert(DATA_INTEGRITY, title=f'Data integrity: {left} to check ({"; ".join(parts)})'[:200],
                       payload={k: v for k, v in result.items() if isinstance(v, dict)},
                       source_type='integrity_scan', source_id=timezone.localdate().isoformat())
