"""Prove the Razorpay / RazorpayX adapters against Razorpay TEST MODE.

    python manage.py razorpay_test_mode_check

Run on QA after the owner adds `rzp_test_` keys. It REFUSES to run with a `rzp_live_` key (or any
key that is not a test key). Everything it creates is a test-mode object:

  payments  create a Rs 1 order; fetch it back; round-trip a locally computed checkout signature
            (valid accepted, tampered rejected)
  payouts   create a test contact and a vpa fund account; create a Rs 1 test payout with an
            idempotency key; send the SAME key again and assert no second payout; fetch the status

It prints a PASS / FAIL / SKIP table. No secret is printed and ids are shortened to a prefix. The
payout steps are SKIPped (not failed) while RAZORPAYX_ACCOUNT_NUMBER is not set. Exit status is 1
if any step failed.
"""

import time

from django.core.management.base import BaseCommand, CommandError

from integrations import razorpay_http as rzp
from integrations.payments import registry
from servers.payments.payment_gateways.base_gateway import PayoutDispatchUnknown


def _short(value):
    text = str(value or '')
    return (text[:8] + '...') if len(text) > 8 else text


class Command(BaseCommand):
    help = 'Exercise the Razorpay and RazorpayX adapters against Razorpay test mode (test keys only).'

    def handle(self, *args, **options):
        pay = registry.adapter_class('payments', 'razorpay')()
        payout = registry.adapter_class('payouts', 'razorpay')()
        from integrations.payments import razorpay as pay_mod
        from integrations.payouts import razorpayx as x_mod

        pay_id, _ = pay_mod.key_pair()
        x_id, _ = x_mod.key_pair()
        for label, key in (('RAZORPAY_KEY_ID', pay_id), ('RAZORPAYX_KEY_ID', x_id)):
            if key and rzp.key_mode(key) != 'test':
                raise CommandError(f'{label} is not a rzp_test_ key: refusing to run '
                                   '(this check only ever touches test mode).')
        if rzp.key_mode(pay_id) != 'test':
            raise CommandError('RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET (rzp_test_) are not set.')
        if not pay:
            raise CommandError(f'Razorpay payments adapter is not usable: {pay.not_configured_reason()}')

        rows = []

        def record(step, ok, detail=''):
            rows.append((step, 'PASS' if ok else 'FAIL', detail))
            return ok

        def skip(step, why):
            rows.append((step, 'SKIP', why))

        stamp = int(time.time())

        # ---- payments ----
        order = pay.create_order(100, trip_id=None, currency='INR', receipt=f'chk_{stamp}',
                                 customer_id='check')
        record('create order (Rs 1)', bool(order and order.get('order_id')),
               _short(order and order.get('order_id')))
        if order:
            info = pay.get_order_status(order['order_id'])
            record('fetch order', bool(info) and info['order_status'] == 'CREATED',
                   (info or {}).get('order_status', 'no reply'))
            fake_payment = 'pay_check000000000'
            signature = rzp.hmac_hex(pay_mod.key_pair()[1], f"{order['order_id']}|{fake_payment}")
            record('signature round-trip (valid)',
                   pay.verify_payment_signature(order['order_id'], fake_payment, signature))
            record('signature round-trip (tampered rejected)',
                   not pay.verify_payment_signature(order['order_id'], fake_payment, signature[:-1] + '0'
                                                    if signature[-1] != '0' else signature[:-1] + '1'))

        # ---- payouts ----
        if not payout:
            skip('payouts', payout.not_configured_reason())
        else:
            contact = payout.ensure_contact('Test Driver', f'chk_{stamp}')
            record('create contact', bool(contact), _short(contact))
            fund = payout.ensure_fund_account(contact, 'vpa', {'address': 'success@razorpay'}) if contact else None
            record('create vpa fund account', bool(fund), _short(fund))
            if contact and fund:
                reference = f'chk_payout_{stamp}'
                try:
                    first = payout.create_upi_payout(
                        'success@razorpay', 100, reference_id=reference, name='Test Driver',
                        contact_id=contact, fund_account_id=fund)
                    again = payout.create_upi_payout(
                        'success@razorpay', 100, reference_id=reference, name='Test Driver',
                        contact_id=contact, fund_account_id=fund)
                except PayoutDispatchUnknown as exc:
                    first = again = None
                    record('create Rs 1 payout', False, f'outcome unknown: {exc}')
                else:
                    record('create Rs 1 payout', bool(first), (first or {}).get('status', ''))
                    record('same idempotency key -> no second payout',
                           bool(first and again)
                           and first['gateway_reference'] == again['gateway_reference'],
                           _short((again or {}).get('gateway_reference')))
                    status = payout.get_payout_status((first or {}).get('gateway_reference'))
                    record('fetch payout status', bool(status), (status or {}).get('status', 'no reply'))

        width = max(len(r[0]) for r in rows)
        self.stdout.write(f"\n{'step'.ljust(width)}  result  detail")
        for step, result, detail in rows:
            self.stdout.write(f'{step.ljust(width)}  {result:<6}  {detail}')
        failed = [r for r in rows if r[1] == 'FAIL']
        self.stdout.write(f"\n{len(rows) - len(failed)} of {len(rows)} steps passed or skipped"
                          f"{'' if not failed else '; ' + str(len(failed)) + ' FAILED'}")
        if failed:
            raise CommandError('razorpay_test_mode_check: one or more steps failed')
