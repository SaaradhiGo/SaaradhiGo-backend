"""referral_abuse: raised by the module-12 referral reward check, not by the sweep.

When a referee completes the required trips and the referrer and referee share a device, a phone
number or a payout instrument, the reward is blocked and `promotions.referrals` raises this flag
(one per referral). It has no thresholds of its own: the trips required and the rewards belong to
the campaign. It is listed on the Rules tab so a reviewer can see where the flag came from.
"""

from .base import Rule


def evaluate(thresholds, country, now):
    return []                       # event driven: see `promotions.referrals.raise_fraud_flag`


RULE = Rule(
    key='referral_abuse', title='Referral abuse',
    summary='A referral reward was blocked because the two accounts share a device, a phone '
            'number or a payout instrument.',
    data_used='Referral rows; push token (as the device), phone numbers incl. emergency contact, '
              'driver payout UPI / bank account (salted hashes).',
    data_missing='No stable device id is stored (the push token stands in). Rider payment '
                 'instruments are not stored, so a shared card or UPI cannot be matched.',
    params=(), evaluate=evaluate)
