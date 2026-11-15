"""unpaid_pair: one rider's payments keep failing on trips with the same driver (15 Nov 2026).

Since a failed payment still pays the driver (the platform carries the loss), a rider and a driver
working together could take money out that way. This rule is raised when a payment fails
(`payments.unpaid.flag_pair`) rather than by the beat: it reads the failed payment rows between the
two and writes a flag on the rider for review. It blocks nothing and tells no one but the reviewers.
"""

from .base import Rule


def evaluate(th, country, now):  # raised from the payment path, never by the beat
    return []


RULE = Rule(
    key='unpaid_pair', title='Failed payments with one driver', subject_role='rider',
    summary='A rider\'s payments failed on two or more trips with the same driver in 90 days; the '
            'driver was paid each time.',
    data_used='Failed online payment rows (not a switch of method) on trips between the rider and '
              'the driver, in the last 90 days.',
    data_missing='No card fingerprint is stored, so the same card on two rider accounts is not tied '
                 'together. A bank decline is not told apart from a deliberate one.',
    params=(),
    evaluate=evaluate)
