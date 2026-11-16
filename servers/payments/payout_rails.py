"""Which currencies have a payout rail, and what happens to a payout that has none.

THE FACT TODAY
--------------
The only payout rail is Cashfree UPI, and it refuses anything but INR
(`driver.services.initiate_upi_payout`). There is no US rail: choosing one
(Stripe, Adyen, ACH...) is a VENDOR decision for the owner and is a go-live
blocker (AUDIT 27.45). So a USD withdrawal can be requested, scheduled and
APPROVED, but cannot be paid.

THE RULE (module 8, carry-over b)
---------------------------------
An approved withdrawal in a currency with no rail is HELD, not failed:

* `status` stays `approved`;
* `payout_status` becomes `awaiting_rail`;
* NO gateway call is made and the wallet is NOT refunded (the money stays
  withdrawn and the driver sees an approved payout, nothing is lost or paid);
* the console says "No payout rail for USD yet: approved payouts wait here;
  nothing is sent".

RELEASE
-------
`release_awaiting_rail()` is the hook. It dispatches every held payout whose
currency NOW has a rail, through the normal `trigger_payout_creation`. It runs
hourly (`payments.release_awaiting_rail`); when a vendor rail is registered in
`RAILS` below, held payouts leave on the next run with no other change.
"""

import logging

logger = logging.getLogger(__name__)

AWAITING_RAIL = 'awaiting_rail'

#: currency -> rail name. Add a US rail HERE when the owner chooses a vendor.
RAILS = {
    'INR': 'cashfree_upi',
}

NO_RAIL_NOTE = ('No payout rail for {currency} yet: approved payouts wait here; '
                'nothing is sent.')


def has_rail(currency):
    code = str(currency or '').upper()
    if code in RAILS:
        return True
    if code == 'USD':
        # 13 Nov: Stripe Connect is the US rail, but only once it is configured; until then USD
        # payouts stay held (never dispatched to a provider that cannot pay, never refunded).
        # Read from configuration only: no adapter is built just to ask.
        from django.conf import settings

        from integrations import stripe_http
        provider = ((getattr(settings, 'PAYOUT_PROVIDERS', None) or {}).get('US') or '').lower()
        return provider in ('stripe_connect', 'stripe-connect') and             stripe_http.key_refusal(stripe_http.secret_key()) is None
    return False


def rail_note(currency):
    return NO_RAIL_NOTE.format(currency=str(currency or '').upper())


def hold(withdrawal):
    """Mark an approved withdrawal as waiting for a rail. No money moves."""
    withdrawal.payout_status = AWAITING_RAIL
    withdrawal.save(update_fields=['payout_status'])
    logger.warning('payout_awaiting_rail', extra={
        'event': 'payout_awaiting_rail', 'withdrawal_id': withdrawal.id,
        'currency': withdrawal.currency})


def release_awaiting_rail():
    """Dispatch held payouts whose currency now has a rail. Returns the ids sent."""
    from servers.driver.models import WithdrawalRequest
    from servers.driver.services import trigger_payout_creation

    released = []
    held = (WithdrawalRequest.objects
            .filter(status='approved', payout_status=AWAITING_RAIL)
            .select_related('driver', 'driver__user_id'))
    for w in held:
        if not has_rail(w.currency):
            continue
        # The marker is NOT cleared first: a worker dying between "clear" and "send"
        # would orphan an approved payout. `trigger_payout_creation` overwrites
        # payout_status itself on success, and a failure moves the row out of
        # `approved`, so either way it leaves this selection.
        try:
            trigger_payout_creation(w)
            released.append(w.id)
        except Exception:  # noqa: BLE001 - the trigger owns its failure handling
            logger.exception('release_awaiting_rail: trigger failed for %s', w.id)
    return released
