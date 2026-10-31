"""multi_accounting: several rider accounts that look like one person.

Starts from rider accounts created in the window and follows three links to every other rider
account: the same push token (one per install, so the same handset), the same phone number written
differently (`+91 98..` / `098..`), and the same emergency contact (a weaker link: families share
one). Accounts joined by those links form a group; a group of `min_accounts` or more is a hit. The
subject is the group's OLDEST account and the others are `counterpart_user_ids`, so a later joiner
appends to the same flag. First-ride promo redemptions by the group raise the severity.
"""

import re

from django.contrib.auth import get_user_model

from servers.fraud.models import HIGH

from .base import Hit, Param, Rule, clamp_score, country_matches, severity_for, window_start

MAX_NEW_ACCOUNTS = 500
MAX_GROUP = 40
SAME_DEVICE, SAME_PHONE, SAME_EMERGENCY = 'same_device', 'same_phone', 'same_emergency_contact'


def _tail(raw):
    digits = re.sub(r'\D', '', str(raw or ''))[-10:]
    return digits if len(digits) >= 7 else ''


def _neighbours(user, riders):
    """`{other user id: {signal, ...}}` for riders linked to `user`."""
    found = {}

    def add(qs, signal):
        for other in qs.exclude(pk=user.pk).values_list('pk', flat=True)[:MAX_GROUP]:
            found.setdefault(other, set()).add(signal)

    token = (user.fcm_token or '').strip()
    if token:
        add(riders.filter(fcm_token=token), SAME_DEVICE)
    phone, emergency = _tail(user.phone_number), _tail(user.emergency_contact)
    if phone:
        add(riders.filter(phone_number__endswith=phone), SAME_PHONE)
        add(riders.filter(emergency_contact__endswith=phone), SAME_EMERGENCY)
    if emergency:
        add(riders.filter(emergency_contact__endswith=emergency), SAME_EMERGENCY)
        add(riders.filter(phone_number__endswith=emergency), SAME_EMERGENCY)
    return found


def evaluate(th, country, now):
    from servers.ride.models import PromoRedemption

    User = get_user_model()
    riders = User.objects.filter(role='rider')
    fresh = list(riders.filter(created_at__gte=window_start(now, days=th['window_days']))
                 .order_by('-created_at')[:MAX_NEW_ACCOUNTS])
    seen, hits = set(), []
    minimum = th['min_accounts']
    for start in fresh:
        if start.pk in seen:
            continue
        group, signals, queue = {start.pk: start}, set(), [start]
        while queue and len(group) < MAX_GROUP:
            current = queue.pop()
            for other_id, sigs in _neighbours(current, riders).items():
                signals |= sigs
                if other_id not in group and len(group) < MAX_GROUP:
                    group[other_id] = User.objects.get(pk=other_id)
                    queue.append(group[other_id])
        seen |= set(group)
        if len(group) < minimum:
            continue
        members = sorted(group.values(), key=lambda u: (u.created_at, u.pk))
        subject = members[0]
        if not country_matches(subject, country):
            continue
        promos = PromoRedemption.objects.filter(user__in=list(group)).count()
        strong = signals & {SAME_DEVICE, SAME_PHONE}
        score = clamp_score(
            35 + 15 * (len(group) - minimum) + (20 if SAME_DEVICE in signals else 0)
            + (10 if SAME_PHONE in signals else 0) + (15 if promos else 0)
            + (0 if strong else -10))
        severity = HIGH if (SAME_DEVICE in signals and promos) else severity_for(score)
        hits.append(Hit(
            subject=subject, severity=severity, score=score, signals=sorted(signals),
            refs={'counterpart_user_ids': [u.pk for u in members[1:]]},
            measures={'accounts': len(group), 'promo_redemptions': promos}))
    return hits


RULE = Rule(
    key='multi_accounting', title='Multiple accounts', subject_role='rider',
    summary='Several rider accounts share a push token, a phone number or an emergency contact.',
    data_used='Rider accounts created in the window; push token (the device); phone numbers by '
              'their last ten digits; emergency contacts; first-ride promo redemptions.',
    data_missing='No stable device id, advertising id or install id is stored; the push token '
                 'changes on reinstall, so a determined user can slip past it. No payment '
                 'instrument is stored for riders.',
    params=(
        Param('min_accounts', 'Accounts in a group', 'accounts', 2, 50, 3, 3,
              'Smallest group of linked accounts that raises a flag.'),
        Param('window_days', 'Look at accounts created in the last', 'days', 1, 180, 30, 30),
    ),
    evaluate=evaluate)
