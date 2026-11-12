"""Closing an account (13 Nov): what is deleted, what is kept, and pending payouts first.

A rider, or a driver who is owed nothing, is anonymised at once (`erasure.anonymise_user`).
A driver who still has earnings in the wallet or a payout on its way is CLOSING instead
(`AccountClosure`, status `waiting_payouts`): they go offline and cannot go online again; the next
scheduled payout pays the whole balance, with no minimum; and `sweep` (hourly) anonymises the
account once nothing is owed and nothing is in flight. Sign-in stays open until then, so the
driver can watch the payout arrive.
"""

import logging

from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

WAITING, CLOSED = 'waiting_payouts', 'closed'
WEB_URL = 'https://www.saaradhigo.com/delete-account'

DELETED = [
    'Your name, phone number, email, date of birth and address.',
    'Your profile photo, emergency contact and saved places.',
    'For drivers: licence and vehicle document images, and your payout account details.',
    'Your sign-in: every app and website session is signed out.',
]
KEPT = [
    'Trip records (route, times, fare) for safety and disputes, without your name or number.',
    'Payments, payouts and tax records, for the periods tax and accounting law requires.',
    'Anything we must keep to answer a legal request or a safety investigation already open.',
]


def pending_payouts(user):
    """`{balance, currency, in_flight}` while a driver is owed money or has a payout on its way,
    else None. Riders are never owed a payout here (rider credits are not paid out)."""
    from base.money import from_minor
    from servers.driver.models import Driver, WithdrawalRequest
    from servers.payments.weekly_payout import OPEN_STATUSES
    from servers.rider.models import Wallet

    driver = Driver.objects.filter(user_id=user).first()
    if driver is None:
        return None
    wallet = Wallet.objects.filter(user_id=user, scope=Wallet.SCOPE_DRIVER).first()
    balance = wallet.balance_minor if wallet is not None else 0
    in_flight = WithdrawalRequest.objects.filter(driver=driver, status__in=OPEN_STATUSES).count()
    if balance <= 0 and not in_flight:
        return None
    currency = wallet.currency if wallet is not None else 'INR'
    return {'balance': str(from_minor(max(balance, 0), currency)), 'currency': currency,
            'in_flight': in_flight}


def closure_of(user):
    from .models import AccountClosure

    return AccountClosure.objects.filter(user=user, status=WAITING).first()


def is_closing(user_id):
    from .models import AccountClosure

    return AccountClosure.objects.filter(user_id=user_id, status=WAITING).exists()


def preview(user):
    c = closure_of(user)
    return {'deleted': DELETED, 'kept': KEPT, 'pending_payouts': pending_payouts(user),
            'closure': ({'status': c.status, 'requested_at': c.requested_at.isoformat()} if c else None),
            'web_url': WEB_URL}


def request_closure(user, reason, request=None):
    """`('closed', None)` or `('waiting_payouts', pending)`."""
    from .erasure import anonymise_user
    from .models import AccountClosure

    pending = pending_payouts(user)
    with transaction.atomic():
        if pending is None:
            anonymise_user(user, reason=reason or 'self-service DPDP delete',
                           reference=f'self-service user {user.pk}', actor=user, request=request)
            AccountClosure.objects.update_or_create(
                user=user, defaults={'status': CLOSED, 'reason': reason[:1000],
                                     'closed_at': timezone.now()})
            return CLOSED, None
        AccountClosure.objects.update_or_create(
            user=user, defaults={'status': WAITING, 'reason': reason[:1000]})
        from servers.driver.models import Driver
        Driver.objects.filter(user_id=user).update(status='offline')
    logger.warning('account closure waiting for payouts user=%s', user.pk)
    return WAITING, pending


def sweep():
    """Close every waiting account that is now owed nothing. Returns the number closed."""
    from .erasure import AnonymisationRefused, anonymise_user
    from .models import AccountClosure

    closed = 0
    for c in AccountClosure.objects.filter(status=WAITING).select_related('user')[:200]:
        if pending_payouts(c.user) is not None:
            continue
        try:
            with transaction.atomic():
                anonymise_user(c.user, reason=c.reason or 'self-service DPDP delete',
                               reference=f'self-service user {c.user_id} (after payouts)',
                               actor=c.user)
                c.status, c.closed_at = CLOSED, timezone.now()
                c.save(update_fields=['status', 'closed_at'])
            closed += 1
        except AnonymisationRefused:
            logger.exception('account closure refused user=%s', c.user_id)
    return closed
