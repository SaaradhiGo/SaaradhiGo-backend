"""Linked accounts: who else is connected to the subject of a flag, and by what.

Accounts are linked by a shared push token (the nearest thing to a device id the platform stores),
a shared phone number (written differently, or listed as the other's emergency contact), or a
shared payout instrument (drivers: UPI id or bank account). Each linked account is shown as a
MASKED LABEL (`rider #12`), the signals that link it, and its status. No phone, email, token or
account number leaves this module.

SCOPE
-----
An account the caller cannot see (a driver outside their cities, a rider with no anchor in their
scope) is not named: it is counted, and the response says "1 account outside your scope".
"""

import re

from django.contrib.auth import get_user_model

from servers.admin_access.scoping import scoped

MAX_PER_SIGNAL = 25
SAME_DEVICE, SAME_PHONE = 'same_device', 'same_phone'
SAME_EMERGENCY, SAME_INSTRUMENT = 'same_emergency_contact', 'same_instrument'
IN_EVIDENCE = 'named_in_evidence'

SIGNAL_LABELS = {
    SAME_DEVICE: 'same device', SAME_PHONE: 'same phone number',
    SAME_EMERGENCY: 'same emergency contact', SAME_INSTRUMENT: 'same payout instrument',
    IN_EVIDENCE: 'named in the evidence',
}


def _tail(raw):
    digits = re.sub(r'\D', '', str(raw or ''))[-10:]
    return digits if len(digits) >= 7 else ''


def candidates(user):
    """`{other user id: {signal, ...}}` for every account sharing a fingerprint with `user`."""
    User = get_user_model()
    others = User.objects.exclude(pk=user.pk).filter(role__in=('rider', 'driver'))
    found = {}

    def add(qs, signal):
        for pk in qs.values_list('pk', flat=True)[:MAX_PER_SIGNAL]:
            found.setdefault(pk, set()).add(signal)

    token = (user.fcm_token or '').strip()
    if token:
        add(others.filter(fcm_token=token), SAME_DEVICE)
    phone, emergency = _tail(user.phone_number), _tail(user.emergency_contact)
    if phone:
        add(others.filter(phone_number__endswith=phone), SAME_PHONE)
        add(others.filter(emergency_contact__endswith=phone), SAME_EMERGENCY)
    if emergency:
        add(others.filter(phone_number__endswith=emergency), SAME_EMERGENCY)
        add(others.filter(emergency_contact__endswith=emergency), SAME_EMERGENCY)

    driver = getattr(user, 'driver', None) if user.role == 'driver' else None
    if driver is not None:
        from servers.driver.models import Driver, DriverBankAccount, DriverUPIContact

        upis = {u.strip().lower() for u in (
            driver.upi_id, getattr(getattr(driver, 'upi_contact', None), 'upi_id', '')) if u}
        for upi in upis:
            for pk in Driver.objects.exclude(pk=driver.pk).filter(upi_id__iexact=upi).values_list(
                    'user_id_id', flat=True)[:MAX_PER_SIGNAL]:
                found.setdefault(pk, set()).add(SAME_INSTRUMENT)
            for pk in DriverUPIContact.objects.exclude(driver=driver).filter(
                    upi_id__iexact=upi).values_list('driver__user_id_id', flat=True)[:MAX_PER_SIGNAL]:
                found.setdefault(pk, set()).add(SAME_INSTRUMENT)
        bank = getattr(driver, 'bank_account', None)
        if bank is not None and bank.account_number:
            for pk in DriverBankAccount.objects.exclude(driver=driver).filter(
                    account_number=bank.account_number, ifsc_code__iexact=bank.ifsc_code
            ).values_list('driver__user_id_id', flat=True)[:MAX_PER_SIGNAL]:
                found.setdefault(pk, set()).add(SAME_INSTRUMENT)
    return found


def _visible(caller, account):
    """May `caller` see this account? Drivers by home city, riders by the rider scope rule."""
    if account.role == 'driver':
        from servers.driver.models import Driver

        driver = Driver.objects.filter(user_id=account).first()
        return driver is not None and scoped(
            caller, Driver.objects.filter(pk=driver.pk)).exists()
    if account.role == 'rider':
        from servers.rider.models import Rider
        from servers.rider.scope import riders_visible_to

        rider = Rider.objects.filter(user_id=account).first()
        return rider is not None and riders_visible_to(caller).filter(pk=rider.pk).exists()
    return False


def _status(account):
    if not account.is_active:
        return 'inactive'
    if account.role == 'driver':
        from servers.driver.models import DriverSuspension

        if DriverSuspension.objects.filter(driver__user_id=account, lifted_at__isnull=True).exists():
            return 'suspended'
    elif account.role == 'rider':
        from servers.rider.models import RiderBlock

        if RiderBlock.objects.filter(rider__user_id=account, lifted_at__isnull=True).exists():
            return 'suspended'
    return 'active'


def _href(account):
    if account.role == 'driver':
        driver = getattr(account, 'driver', None)
        return f'/drivers/{driver.pk}' if driver is not None else None
    return f'/riders/{account.pk}'


def linked_accounts(flag, caller):
    """`{'accounts': [...], 'outside_scope': n, 'outside_scope_label': str|None}`."""
    User = get_user_model()
    found = {pk: set(sigs) for pk, sigs in candidates(flag.subject).items()}
    for pk in ((flag.evidence or {}).get('refs') or {}).get('counterpart_user_ids') or []:
        if pk != flag.subject_id:
            found.setdefault(int(pk), set()).add(IN_EVIDENCE)
    accounts, outside = [], 0
    for account in User.objects.filter(pk__in=list(found)).order_by('pk'):
        if account.role not in ('rider', 'driver'):
            continue
        if not _visible(caller, account):
            outside += 1
            continue
        signals = sorted(found[account.pk])
        accounts.append({
            'ref': f'{account.role} #{account.pk}', 'role': account.role, 'user_id': account.pk,
            'signals': signals, 'signal_labels': [SIGNAL_LABELS[s] for s in signals],
            'status': _status(account), 'href': _href(account)})
    label = None
    if outside:
        label = f'{outside} account{"s" if outside != 1 else ""} outside your scope'
    return {'accounts': accounts, 'outside_scope': outside, 'outside_scope_label': label}
