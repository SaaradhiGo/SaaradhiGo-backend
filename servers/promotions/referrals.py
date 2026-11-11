"""Referrals: rider -> rider and driver -> driver.

THE RULINGS (owner, 2026-10-31, AUDIT 27.51)
--------------------------------------------
* A reward is earned ONLY AFTER THE REFEREE COMPLETES N TRIPS. N, and the reward for the
  referrer and for the referee, are set on the campaign, which belongs to one country, so each
  country configures its own.
* NO REWARD ACROSS THE SAME DEVICE, PHONE NUMBER OR PAYMENT INSTRUMENT. A blocked reward is
  recorded on the referral, raises a fraud-queue item (`fraud.RiskFlag`) and, for a rider, the
  existing `RiderFlag('promo_abuse')` so the Riders page agrees.
* Each campaign has a BUDGET CAP. A reward that does not fit is not paid; the referral stays
  `qualified` and is retried if the budget is increased.
* Rewards: a rider's is a wallet credit (`issue_promo_credit`); a driver's is a ledger credit
  `referral_reward` in the driver's settlement wallet, so the weekly payout carries it, with no
  commission.

WHAT THE PLATFORM STORES, AND SO WHAT CAN BE COMPARED
-----------------------------------------------------
  device      No device id is stored anywhere. The only per-install value is the push token
              (`CustomUser.fcm_token`), which is one per install: two accounts holding the same
              token are on one handset. The app may also send a `device_id` when the referee
              attaches; only a salted HASH of it is kept (`Referral.device_hash`) and it is
              compared with the hash of the referrer's push token and device hash. Today that
              is the push token; the apps' work item is to send a stable device id.
  phone       `CustomUser.phone_number` is unique per account, so two accounts can share a
              phone only by formatting (`+91 98..`, `098..`) or through `emergency_contact`.
              Both are normalised (last ten digits) and compared, including one account's
              emergency contact against the other's number.
  instrument  Driver: the payout UPI id (`Driver.upi_id`, `DriverUPIContact.upi_id`) and bank
              account (`DriverBankAccount.account_number`), compared as salted hashes.
              Rider: NOTHING is stored (the gateway order response carries no card or UPI
              fingerprint), so the rider check cannot fire. A work item for the gateway
              integration, listed in the module report.
Evidence stored on a flag names WHICH signal matched, never the value.
"""

import hashlib
import logging
import re
import secrets

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from . import budget
from .models import (
    ATTACHED, BLOCKED, LIVE, QUALIFIED, REFERRAL, REFERRAL_REWARD, REWARDED, Campaign,
    LedgerEntry, Referral, ReferralCode, ReferralTrip,
)

logger = logging.getLogger(__name__)

CODE_ALPHABET = 'ABCDEFGHJKMNPQRSTUVWXYZ23456789'     # no 0/O, 1/I/L
CODE_LENGTH = 8
SIGNAL_DEVICE, SIGNAL_PHONE, SIGNAL_INSTRUMENT = 'same_device', 'same_phone', 'same_instrument'


class AttachRefused(Exception):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


# ---------------------------------------------------------------------------
# Codes
# ---------------------------------------------------------------------------

def code_for(user):
    """The user's shareable code, created on first ask."""
    existing = ReferralCode.objects.filter(user=user).first()
    if existing:
        return existing
    for _ in range(10):
        code = ''.join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
        try:
            with transaction.atomic():
                return ReferralCode.objects.create(user=user, code=code)
        except IntegrityError:
            found = ReferralCode.objects.filter(user=user).first()
            if found:
                return found
    raise RuntimeError('could not allocate a referral code')


def _role(user):
    return user.role if user.role in ('rider', 'driver') else ''


# ---------------------------------------------------------------------------
# Fingerprints (hashes only; evidence never carries a value)
# ---------------------------------------------------------------------------

def _salted(value):
    value = (value or '').strip()
    if not value:
        return ''
    salt = getattr(settings, 'SECRET_KEY', '')
    return hashlib.sha256(f'referral:{salt}:{value}'.encode()).hexdigest()


def _digits(raw):
    return re.sub(r'\D', '', str(raw or ''))[-10:]


def device_hash(user, supplied=''):
    """Hash of the device id the app sent, else of the push token, else ''."""
    return _salted(supplied) or _salted(getattr(user, 'fcm_token', '') or '')


def device_hashes(user, referral=None):
    out = {_salted(getattr(user, 'fcm_token', '') or '')}
    if referral is not None and referral.referee_id == user.pk:
        out.add(referral.device_hash)
    return {h for h in out if h}


def phone_keys(user):
    return {k for k in (_digits(user.phone_number), _digits(user.emergency_contact)) if len(k) >= 7}


def instrument_hashes(user):
    """Payout instruments of a DRIVER, hashed. A rider has none stored (see the docstring)."""
    out = set()
    driver = getattr(user, 'driver', None) if user.role == 'driver' else None
    if driver is None:
        return out
    if driver.upi_id:
        out.add(_salted('upi:' + driver.upi_id.strip().lower()))
    contact = getattr(driver, 'upi_contact', None)
    if contact is not None and contact.upi_id:
        out.add(_salted('upi:' + contact.upi_id.strip().lower()))
    bank = getattr(driver, 'bank_account', None)
    if bank is not None and bank.account_number:
        out.add(_salted(f'bank:{bank.ifsc_code.strip().upper()}:{bank.account_number.strip()}'))
    return {h for h in out if h}


def abuse_signals(referrer, referee, referral=None):
    """Which of device / phone / instrument the two accounts share, as signal names."""
    signals = []
    if device_hashes(referrer) & device_hashes(referee, referral):
        signals.append(SIGNAL_DEVICE)
    elif referral is not None and referral.device_hash and referral.device_hash in device_hashes(referrer):
        signals.append(SIGNAL_DEVICE)
    if phone_keys(referrer) & phone_keys(referee):
        signals.append(SIGNAL_PHONE)
    if instrument_hashes(referrer) & instrument_hashes(referee):
        signals.append(SIGNAL_INSTRUMENT)
    return signals


# ---------------------------------------------------------------------------
# Campaign selection
# ---------------------------------------------------------------------------

def live_campaign_for(user):
    """The live referral campaign whose place covers `user`, most specific first."""
    from servers.notifications import recipients

    role = _role(user)
    if not role:
        return None
    node = recipients.city_of(user)
    country = recipients.country_of(user)
    best = None
    for campaign in (Campaign.objects.filter(kind=REFERRAL, state=LIVE, is_legacy=False,
                                             country=country or '')
                     .select_related('geo_node').order_by('-id')):
        if (campaign.config or {}).get('audience') != role:
            continue
        place = campaign.geo_node
        if place is not None and place.kind != 'country' and (
                node is None or not str(node.path).startswith(place.path)):
            continue
        depth = 0 if place is None or place.kind == 'country' else 1
        if best is None or depth > best[0]:
            best = (depth, campaign)
    return best[1] if best else None


def available_in_market(country_code, audience, city_node=None):
    """Is a referral offer live for a `rider` or `driver` in this market right now?

    The app-config `referrals_available` flag: the apps show their referral entry (and the rider's
    sign-up code field) only when this is true. It mirrors `live_campaign_for`: a `live`,
    non-legacy referral campaign for this audience, in this country, anchored at the country or at
    the named city (or an ancestor of it). With no city named, a campaign anchored at any city of
    the country counts. This only SHOWS an entry; attach still decides.
    """
    if audience not in ('rider', 'driver'):
        return False
    for campaign in (Campaign.objects.filter(kind=REFERRAL, state=LIVE, is_legacy=False,
                                             country=country_code or '')
                     .select_related('geo_node')):
        if (campaign.config or {}).get('audience') != audience:
            continue
        place = campaign.geo_node
        if (city_node is None or place is None or place.kind in ('country', 'global')
                or str(city_node.path).startswith(str(place.path))):
            return True
    return False


# ---------------------------------------------------------------------------
# Attribution
# ---------------------------------------------------------------------------

def attach(referee, code, device_id=''):
    """Attribute `referee` to the owner of `code`. One referrer per person, ever."""
    code = (code or '').strip().upper()
    if not code:
        raise AttachRefused('CODE_REQUIRED', 'A referral code is required.')
    role = _role(referee)
    if not role:
        raise AttachRefused('NOT_ELIGIBLE', 'Only riders and drivers can use a referral code.', 403)
    owner_code = ReferralCode.objects.select_related('user').filter(code=code).first()
    if owner_code is None or owner_code.user.role != role or not owner_code.user.is_active:
        raise AttachRefused('CODE_NOT_FOUND', 'That referral code does not exist.', 404)
    referrer = owner_code.user
    if referrer.pk == referee.pk:
        raise AttachRefused('SELF_REFERRAL', 'You cannot use your own referral code.')
    if Referral.objects.filter(referee=referee).exists():
        raise AttachRefused('ALREADY_REFERRED', 'A referral code was already used on this account.', 409)
    if _has_completed_trips(referee):
        raise AttachRefused('TOO_LATE', 'A referral code must be used before your first trip.', 409)
    campaign = live_campaign_for(referee)
    if campaign is None:
        raise AttachRefused('NO_ACTIVE_CAMPAIGN',
                            'There is no referral offer running for you right now.', 409)
    try:
        with transaction.atomic():
            return Referral.objects.create(
                campaign=campaign, referrer=referrer, referee=referee, code_used=code,
                currency=campaign.currency, device_hash=device_hash(referee, device_id))
    except IntegrityError:
        raise AttachRefused('ALREADY_REFERRED', 'A referral code was already used on this account.', 409)


def _has_completed_trips(user):
    from servers.ride.models import Trip

    if user.role == 'driver':
        return Trip.objects.filter(driver_id__user_id=user,
                                   status_id__status_code='completed').exists()
    return Trip.objects.filter(user_id=user, status_id__status_code='completed').exists()


# ---------------------------------------------------------------------------
# Counting trips, qualifying, rewarding
# ---------------------------------------------------------------------------

def on_trip_completed(trip):
    """Count `trip` toward the referee's referral, if this trip's person has one."""
    if getattr(trip, 'end_rule', ''):
        return None
    person = (trip.driver_id.user_id if trip.driver_id_id is not None else None)
    results = []
    for user in (trip.user_id, person):
        if user is None:
            continue
        referral = Referral.objects.filter(referee=user, status=ATTACHED).first()
        # A rider referral counts the referee's RIDES; a driver referral counts the trips they
        # DROVE. The campaign's audience says which.
        if referral is None or (referral.campaign.config or {}).get('audience') != _role(user):
            continue
        results.append(count_trip(referral, trip))
    return results


def count_trip(referral, trip):
    with transaction.atomic():
        referral = Referral.objects.select_for_update().select_related('campaign').get(
            pk=referral.pk)
        if referral.status != ATTACHED:
            return referral
        try:
            with transaction.atomic():
                ReferralTrip.objects.create(referral=referral, trip=trip)
        except IntegrityError:
            return referral                      # a replay counts nothing twice
        referral.trips_completed = ReferralTrip.objects.filter(referral=referral).count()
        referral.save(update_fields=['trips_completed', 'updated_at'])
        required = int((referral.campaign.config or {}).get('required_trips') or 0)
        if required and referral.trips_completed >= required:
            return qualify(referral)
    return referral


def qualify(referral):
    """The threshold is met: block on abuse, else reward. Call with the referral row locked."""
    referral.status = QUALIFIED
    signals = abuse_signals(referral.referrer, referral.referee, referral)
    if signals:
        return block(referral, signals)
    return pay(referral)


def block(referral, signals):
    referral.status = BLOCKED
    referral.blocked_signals = list(signals)
    referral.blocked_at = timezone.now()
    referral.save(update_fields=['status', 'blocked_signals', 'blocked_at', 'updated_at'])
    raise_fraud_flag(referral, signals)
    return referral


def raise_fraud_flag(referral, signals):
    """One fraud-queue item per blocked referral, plus the Riders page flag for a rider."""
    from servers.fraud.models import REFERRAL_ABUSE, RiskFlag
    from servers.notifications import recipients

    node = recipients.city_of(referral.referrer)
    # A shared device or payout instrument is strong evidence; a shared phone number alone less so.
    strong = bool({SIGNAL_DEVICE, SIGNAL_INSTRUMENT} & set(signals))
    now = timezone.now()
    flag, created = RiskFlag.objects.get_or_create(
        kind=REFERRAL_ABUSE, source_ref=f'referral:{referral.pk}',
        defaults=dict(
            subject=referral.referrer, anchor_node=node, rule_key=REFERRAL_ABUSE, rule_version=1,
            severity='high' if strong else 'medium', score=80 if strong else 60, last_seen_at=now,
            evidence={'referral_id': referral.pk, 'campaign_id': referral.campaign_id,
                      'role': _role(referral.referrer), 'signals': list(signals),
                      'refs': {'counterpart_user_ids': [referral.referee_id],
                               'campaign_ids': [referral.campaign_id]},
                      'referee_user_id': referral.referee_id,
                      'trips_completed': referral.trips_completed,
                      'hit_count': 1, 'first_seen': now.isoformat(), 'last_seen': now.isoformat()}))
    if created:
        from servers.fraud.models import EV_RAISED, FlagEvent
        from servers.fraud.raising import alert_high

        FlagEvent.objects.create(flag=flag, action=EV_RAISED, to_status=flag.status,
                                 data={'rule': REFERRAL_ABUSE, 'score': flag.score,
                                       'severity': flag.severity})
        if flag.severity == 'high':
            transaction.on_commit(lambda: alert_high(flag.pk))
        from servers.fraud.raising import hold_rewards

        hold_rewards(flag)
    if _role(referral.referrer) == 'rider':
        from servers.rider.models import Rider, RiderFlag

        rider = Rider.objects.filter(user_id=referral.referrer).first()
        if rider is not None and not RiderFlag.objects.filter(
                rider=rider, reason='promo_abuse', cleared_at__isnull=True).exists():
            RiderFlag.objects.create(
                rider=rider, reason='promo_abuse',
                note=f'Referral {referral.pk} reward blocked: {", ".join(signals)}')


def pay(referral):
    """Reserve the budget and pay both rewards, atomically. `qualified` if it cannot be paid."""
    campaign = referral.campaign
    cfg = campaign.config or {}
    referrer_minor = int(cfg.get('referrer_reward_minor') or 0)
    referee_minor = int(cfg.get('referee_reward_minor') or 0)
    total = referrer_minor + referee_minor
    if total <= 0:
        referral.status = REWARDED
        referral.rewarded_at = timezone.now()
        referral.save(update_fields=['status', 'rewarded_at', 'updated_at'])
        return referral
    try:
        with transaction.atomic():
            budget.spend_now(campaign.pk, total)
            for user, minor, who in ((referral.referrer, referrer_minor, 'referrer'),
                                     (referral.referee, referee_minor, 'referee')):
                if minor > 0:
                    _credit(referral, campaign, user, minor, who)
            referral.status = REWARDED
            referral.referrer_reward_minor = referrer_minor
            referral.referee_reward_minor = referee_minor
            referral.rewarded_at = timezone.now()
            referral.save(update_fields=['status', 'referrer_reward_minor',
                                         'referee_reward_minor', 'rewarded_at', 'updated_at'])
    except (budget.Refused, _CreditRefused) as exc:
        # Nothing was paid and nothing was spent (the atomic block rolled back). The referral
        # stays qualified; the sweep retries it, e.g. after an approved budget increase.
        referral.status = QUALIFIED
        referral.save(update_fields=['status', 'updated_at'])
        logger.info('referral_unpaid referral=%s reason=%s', referral.pk, exc)
        return referral
    _notify(referral)
    return referral


class _CreditRefused(Exception):
    pass


def _credit(referral, campaign, user, minor, who):
    from base.money import from_minor
    from servers.rider.credits import issue_promo_credit
    from servers.rider.models import Wallet, WalletTransaction, get_wallet

    key = f'referral:{referral.pk}:{who}'
    txn = None
    if _role(user) == 'rider':
        result = issue_promo_credit(user, from_minor(minor, campaign.currency),
                                    f'referral:{campaign.pk}', key)
        if not result.ok:
            raise _CreditRefused(result.reason)
        txn = WalletTransaction.objects.filter(pk=result.transaction_id).first()
    else:
        wallet = get_wallet(user, Wallet.SCOPE_DRIVER, lock=True)
        txn = WalletTransaction.objects.create(
            user_id=user, currency=campaign.currency, amount_minor=minor, txn_type='credit',
            status='completed', purpose=REFERRAL_REWARD, reference_id=f'REFERRAL_{referral.pk}',
            idempotency_key=key)
        wallet.balance_minor = wallet.balance_minor + minor
        wallet.save(update_fields=['balance_minor'])
    LedgerEntry.objects.create(
        kind=REFERRAL_REWARD, campaign=campaign, user=user, amount_minor=minor,
        currency=campaign.currency, wallet_transaction=txn, idempotency_key=key)


def _notify(referral):
    from base.money import format_money, from_minor
    from servers.notifications import dispatcher

    currency = referral.campaign.currency
    for user, minor, who in ((referral.referrer, referral.referrer_reward_minor, 'referrer'),
                             (referral.referee, referral.referee_reward_minor, 'referee')):
        if minor <= 0:
            continue
        try:
            dispatcher.notify(
                'promotions.referral_reward', user,
                {'reward': format_money(from_minor(minor, currency), currency)},
                idempotency_key=f'promotions.referral_reward:{referral.pk}:{who}',
                source='promotions.referral_reward')
        except Exception:  # noqa: BLE001 - a notification must never undo a reward
            logger.exception('referral notification failed referral=%s', referral.pk)


def retry_qualified(limit=200):
    """The sweep: pay referrals that qualified but could not be paid (budget, wallet cap)."""
    paid = 0
    for referral in (Referral.objects.filter(status=QUALIFIED, campaign__state=LIVE)
                     .select_related('campaign', 'referrer', 'referee')[:limit]):
        with transaction.atomic():
            locked = Referral.objects.select_for_update().select_related(
                'campaign', 'referrer', 'referee').get(pk=referral.pk)
            if locked.status == QUALIFIED:
                paid += 1 if pay(locked).status == REWARDED else 0
    return paid


# ---------------------------------------------------------------------------
# What the apps read
# ---------------------------------------------------------------------------

def _initials(user):
    name = (user.full_name or '').strip()
    if not name:
        return 'A new member'
    parts = name.split()
    return parts[0] + (f' {parts[-1][0]}.' if len(parts) > 1 else '')


def mine(user):
    from base.money import format_money, from_minor

    code = code_for(user)
    campaign = live_campaign_for(user)
    out = {'code': code.code, 'offer': None, 'referrals': [], 'attached_to': None}
    if campaign is not None:
        cfg = campaign.config or {}
        out['offer'] = {
            'campaign_id': campaign.pk, 'name': campaign.name,
            'required_trips': int(cfg.get('required_trips') or 0),
            'referrer_reward_minor': int(cfg.get('referrer_reward_minor') or 0),
            'referee_reward_minor': int(cfg.get('referee_reward_minor') or 0),
            'currency': campaign.currency,
            'referrer_reward': format_money(
                from_minor(int(cfg.get('referrer_reward_minor') or 0), campaign.currency),
                campaign.currency),
            'referee_reward': format_money(
                from_minor(int(cfg.get('referee_reward_minor') or 0), campaign.currency),
                campaign.currency),
        }
    from servers.fraud.models import RewardHold

    held_keys = set(RewardHold.objects.filter(
        user=user, status=RewardHold.HELD, kind=REFERRAL_REWARD).values_list(
        'ledger_entry__idempotency_key', flat=True))
    for r in (Referral.objects.filter(referrer=user).select_related('referee', 'campaign')
              .order_by('-created_at')[:100]):
        required = int((r.campaign.config or {}).get('required_trips') or 0)
        out['referrals'].append({
            'id': r.pk, 'who': _initials(r.referee), 'status': r.status,
            'trips_completed': r.trips_completed, 'required_trips': required,
            'reward_minor': r.referrer_reward_minor, 'currency': r.campaign.currency,
            # A blocked reward says only that it is under review, never why.
            'note': 'under review' if r.status == BLOCKED else '',
            # A reward held for review reads "pending review", never why (additive).
            'reward_status': ('pending_review' if f'referral:{r.pk}:referrer' in held_keys
                              else None),
            'joined_at': r.created_at.isoformat()})
    attached = Referral.objects.filter(referee=user).select_related('campaign').first()
    if attached is not None:
        required = int((attached.campaign.config or {}).get('required_trips') or 0)
        out['attached_to'] = {
            'status': 'under review' if attached.status == BLOCKED else attached.status,
            'trips_completed': attached.trips_completed, 'required_trips': required}
    return out
