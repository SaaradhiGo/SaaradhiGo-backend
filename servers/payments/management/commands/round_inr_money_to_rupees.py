"""Round stored India trip money to whole rupees (owner, 23 Nov: "India money in whole rupees end to
end ... correct stored QA data").

    python manage.py round_inr_money_to_rupees                                   # dry run: what changes
    python manage.py round_inr_money_to_rupees --driver 59                       # dry run, one driver
    python manage.py round_inr_money_to_rupees --confirm --actor-email you@...   # write, audited

India only (trips in an `IN` zone, in INR); USD is never touched. In one transaction:

  * every COMPLETED trip whose money carries paise: the trip's `estimated_fare_minor` /
    `final_fare_minor` -> whole rupees, half up; each settlement version -> gross whole, commission
    = whole rupees of gross x the percent it applied (half up), net = gross - commission. A trip
    with a ledger row but no settlement (older QA data) has its commission rounded half up.
  * the driver's ledger is append-only (AUDIT 27.76), so no ledger row is edited: one
    `whole_rupee_correction` line per trip (`TRIP_<id>`) moves the balance by exactly the difference
    between what the driver's screens showed and the whole-rupee figures. A platform-fee refund for
    the trip is folded into the same line. Earnings, Trips and the statement read it
    (`driver.earnings._apply_whole_rupee_corrections`).
  * each INR `DriverDailyFee` with paise is floored to a whole rupee (a fee never rises above the
    legal cap it was held to; a refund of the whole day follows it), with one `whole_rupee_correction`
    line per day (`DAY_<yyyymmdd>`) for the difference.

Left alone on purpose: rider payments and wallet top-ups (amounts a gateway reported, in paise), the
original ledger and `TransactionHistory` rows (append-only), issued receipts and their fare snapshots
(versioned tax documents), cancellation penalties.

One audit row per corrected trip and fee day, with before and after, and one summary row. A second
run finds nothing to do.
"""

from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal

from django.core.management.base import BaseCommand
from django.db import IntegrityError, transaction

from servers.admin_audit.actor_option import actor_label, add_actor_argument, require_actor
from servers.admin_audit.context import audit_context
from servers.admin_audit.services import record_admin_action

COMMAND = 'round_inr_money_to_rupees'
CURRENCY = 'INR'
COUNTRY = 'IN'
PURPOSE = 'whole_rupee_correction'
STEP = 100  # paise in a rupee (base.money CURRENCIES['INR'].exponent == 2)
REASON = 'Owner, 23 Nov: India money in whole rupees end to end; stored QA paise rounded.'


def _half_up(minor):
    return int((Decimal(int(minor)) / STEP).quantize(Decimal('1'), rounding=ROUND_HALF_UP)) * STEP


def _floor(minor):
    return int((Decimal(int(minor)) / STEP).quantize(Decimal('1'), rounding=ROUND_FLOOR)) * STEP


def _commission(gross_minor, percent):
    return _half_up((Decimal(int(gross_minor)) * Decimal(str(percent)) / Decimal('100'))
                    .quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def _signed(txn_type, minor):
    return minor if txn_type == 'credit' else -minor


class Command(BaseCommand):
    help = 'Round stored INR trip money (fares, settlements, driver earnings, platform fees) to whole rupees.'

    def add_arguments(self, parser):
        parser.add_argument('--confirm', action='store_true', help='Write. Without it, a dry run.')
        parser.add_argument('--driver', type=int, default=None, help='Only this driver id.')
        add_actor_argument(parser)

    # -- planning: nothing below writes ----------------------------------------------------------

    def _trip_plans(self, driver_id):
        from servers.driver.earnings import PURPOSE_CASH, SETTLEMENT_PURPOSES
        from servers.payments.models import TripSettlement
        from servers.pricing.fee_plans import REFUND_PURPOSE
        from servers.ride.models import Trip
        from servers.rider.models import WalletTransaction

        trips = (Trip.objects.filter(status_id__status_code='completed', zone__country=COUNTRY,
                                     currency=CURRENCY, driver_id__isnull=False)
                 .select_related('driver_id').order_by('id'))
        if driver_id:
            trips = trips.filter(driver_id_id=driver_id)
        plans = []
        for trip in trips:
            user_id = trip.driver_id.user_id_id
            ref = f'TRIP_{trip.id}'
            settlements = list(TripSettlement.objects.filter(trip=trip, currency=CURRENCY).order_by('version'))
            entry = (WalletTransaction.objects.filter(user_id=user_id, reference_id=ref, currency=CURRENCY,
                                                      purpose__in=SETTLEMENT_PURPOSES, status='completed')
                     .order_by('id').first())
            others = list(WalletTransaction.objects.filter(
                user_id=user_id, reference_id=ref, currency=CURRENCY, status='completed',
                purpose__in=(REFUND_PURPOSE, PURPOSE)).values_list('purpose', 'txn_type', 'amount_minor'))
            refund_old = sum(_signed(t, m) for p, t, m in others if p == REFUND_PURPOSE)
            corrected = sum(_signed(t, m) for p, t, m in others if p == PURPOSE)
            fare_old = trip.final_fare_minor if trip.final_fare_minor is not None else trip.estimated_fare_minor
            fare_old = int(fare_old or 0)
            fare_new = _half_up(fare_old)

            # The settlement versions, rounded.
            new_versions = []
            for s in settlements:
                gross = _half_up(s.gross_fare_minor)
                commission = _commission(gross, s.commission_percent)
                new_versions.append((s, (s.gross_fare_minor, s.commission_amount_minor, s.driver_net_minor),
                                     (gross, commission, gross - commission)))

            # What the driver's screens show today (the reader: net from the ledger row, commission =
            # fare - net, a refund and any earlier correction applied), and what they should show.
            correction, shown = 0, None
            if entry is not None:
                is_cash = entry.purpose == PURPOSE_CASH
                charged_old = entry.amount_minor if is_cash else max(fare_old - entry.amount_minor, 0)
                charged_new = new_versions[-1][2][1] if new_versions else _half_up(charged_old)
                refund_new = (charged_new if refund_old and refund_old >= charged_old
                              else min(_half_up(refund_old), charged_new))
                # Net as read once the trip's fare is whole: a cash trip reads fare - commission.
                net_read = (((fare_new - entry.amount_minor) if is_cash else entry.amount_minor)
                            + refund_old + corrected)
                net_target = fare_new - (charged_new - refund_new)
                correction = net_target - net_read
                shown = {'net_shown': net_read, 'net_whole': net_target, 'commission_whole': charged_new - refund_new}

            fare_changes = {name: (getattr(trip, name), _half_up(getattr(trip, name)))
                            for name in ('estimated_fare_minor', 'final_fare_minor')
                            if getattr(trip, name) is not None and getattr(trip, name) % STEP}
            version_changes = [v for v in new_versions if v[1] != v[2]]
            if not (fare_changes or version_changes or correction):
                continue
            plans.append({'trip': trip, 'user_id': user_id, 'fare': fare_changes, 'settlements': version_changes,
                          'correction': correction, 'shown': shown})
        return plans

    def _fee_plans(self, driver_id):
        from servers.pricing.fee_plan_models import DriverDailyFee
        from servers.pricing.fee_plans import REFUND_PURPOSE
        from servers.rider.models import WalletTransaction

        days = DriverDailyFee.objects.filter(currency=CURRENCY).select_related('driver').order_by('id')
        if driver_id:
            days = days.filter(driver_id=driver_id)
        out = []
        for d in days:
            if not d.fee_minor % STEP:
                continue
            new = _floor(d.fee_minor)
            ref = f'DAY_{d.day:%Y%m%d}'
            rows = list(WalletTransaction.objects.filter(
                user_id=d.driver.user_id_id, reference_id=ref, currency=CURRENCY, status='completed',
                purpose__in=(REFUND_PURPOSE, PURPOSE)).values_list('purpose', 'txn_type', 'amount_minor'))
            refunded = any(p == REFUND_PURPOSE for p, _t, _m in rows)
            # The fee as the screens read it: the debit, less a refund of the day, less any correction.
            charged_old = d.fee_minor if d.wallet_transaction_id else 0
            read = charged_old - sum(_signed(t, m) for _p, t, m in rows)
            target = 0 if refunded or not d.wallet_transaction_id else new
            out.append({'day': d, 'user_id': d.driver.user_id_id, 'ref': ref, 'old': d.fee_minor, 'new': new,
                        'correction': read - target})
        return out

    # -- the run ---------------------------------------------------------------------------------

    def handle(self, *args, **options):
        trips = self._trip_plans(options['driver'])
        days = self._fee_plans(options['driver'])

        for p in trips:
            t = p['trip']
            parts = [f'{k} {o}->{n}' for k, (o, n) in p['fare'].items()]
            parts += [f'settlement v{s.version} {o}->{n}' for s, o, n in p['settlements']]
            if p['correction']:
                parts.append(f"earnings correction {p['correction']:+d}")
            self.stdout.write(f'trip {t.id} (driver {t.driver_id_id}): ' + '; '.join(parts))
        for d in days:
            self.stdout.write(f"platform fee driver {d['day'].driver_id} {d['day'].day}: {d['old']}->{d['new']}"
                              f" (correction {d['correction']:+d})")
        self.stdout.write(f'{len(trips)} trip(s), {len(days)} fee day(s) carry paise (amounts in paise).')
        if not options['confirm']:
            self.stdout.write('Dry run. Add --confirm --actor-email to write.')
            return
        actor = require_actor(options, COMMAND)
        with audit_context(actor=actor, actor_label=actor_label(actor, COMMAND), reason=REASON), \
                transaction.atomic():
            moved = self._write(trips, days)
            record_admin_action(None, action='inr_money_rounded_to_rupees', target_type='trip', target_id='all',
                                before={'trips': len(trips), 'fee_days': len(days)},
                                after={'wallets_moved': moved}, reason=REASON)
        self.stdout.write(f'Rounded {len(trips)} trip(s) and {len(days)} fee day(s); '
                          f'{moved} driver balance(s) moved by a correction line.')

    def _write(self, trips, days):
        from django.contrib.auth import get_user_model

        from servers.rider.models import Wallet, WalletTransaction, get_wallet

        wallets, moved = {}, set()

        def correct(user_id, reference, change, key):
            """One append-only correction line and the balance with it, in the same transaction."""
            if not change:
                return
            if user_id not in wallets:
                wallets[user_id] = get_wallet(get_user_model().objects.get(pk=user_id), Wallet.SCOPE_DRIVER,
                                              lock=True)
            wallet = wallets[user_id]
            try:
                with transaction.atomic():
                    WalletTransaction.objects.create(
                        user_id_id=user_id, currency=CURRENCY, amount_minor=abs(change),
                        txn_type='credit' if change > 0 else 'debit', status='completed', purpose=PURPOSE,
                        reference_id=reference, idempotency_key=key)
            except IntegrityError:
                return  # already written by an earlier run
            wallet.balance_minor = wallet.balance_minor + change
            wallet.save(update_fields=['balance_minor'])
            moved.add(user_id)

        for p in trips:
            t = p['trip']
            if p['fare']:
                type(t).objects.filter(pk=t.pk).update(**{k: n for k, (_o, n) in p['fare'].items()})
            for s, _old, (gross, commission, net) in p['settlements']:
                # QA data, pre-launch: the settlement's figures are corrected in place (audited below),
                # so the console's settlement totals, which sum every version, stay right.
                type(s).objects.filter(pk=s.pk).update(
                    gross_fare_minor=gross, commission_amount_minor=commission, driver_net_minor=net)
            correct(p['user_id'], f'TRIP_{t.id}', p['correction'], f'WHOLE_RUPEE_TRIP_{t.id}')
            record_admin_action(
                None, action='inr_trip_money_rounded', target_type='trip', target_id=str(t.id),
                before={'fare': {k: o for k, (o, _n) in p['fare'].items()},
                        'settlements': {s.version: list(o) for s, o, _n in p['settlements']},
                        'net_shown': p['shown'] and p['shown']['net_shown']},
                after={'fare': {k: n for k, (_o, n) in p['fare'].items()},
                       'settlements': {s.version: list(n) for s, _o, n in p['settlements']},
                       'net_whole': p['shown'] and p['shown']['net_whole'],
                       'commission_whole': p['shown'] and p['shown']['commission_whole'],
                       'correction_minor': p['correction']},
                reason=REASON)
        for d in days:
            row = d['day']
            type(row).objects.filter(pk=row.pk).update(fee_minor=d['new'])
            correct(d['user_id'], d['ref'], d['correction'], f"WHOLE_RUPEE_DAY_{row.driver_id}_{row.day:%Y%m%d}")
            record_admin_action(None, action='inr_platform_fee_rounded', target_type='driver_daily_fee',
                                target_id=str(row.pk), before={'fee_minor': d['old']},
                                after={'fee_minor': d['new'], 'correction_minor': d['correction']}, reason=REASON)
        return len(moved)
