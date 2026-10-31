"""The English templates module 11 ships, for the two system events it wires.

    commission.increase_notice   a commission increase is coming (drains DriverNotice)
    payout.missing_details       the weekly payout skipped you: add payout details
    trips.operator_cancelled     support cancelled a stale trip the driver never reached (rider)

The first two are TRANSACTIONAL, for India and the United States, on in_app + push + sms +
email. The rider cancellation notice is on in_app + push only (no SMS, no email).

INDIA SMS ARE DRAFTS, ON PURPOSE. An India SMS template cannot be active without its DLT
template id, sender id and flow id (the activation rule). Those come from the DLT registry and
MSG91, which only the owner's accounts can produce, so the India SMS rows are created as DRAFT
with the registration row pre-filled with the exact text to submit for DLT approval
(`registered_text`, with `{#var#}` slots) and the three ids blank. That is the rule working: the
templates go live through the console, and a checker, once the ids exist.
"""

from . import templating

COMMISSION = 'commission.increase_notice'
PAYOUT = 'payout.missing_details'
OPERATOR_CANCELLED = 'trips.operator_cancelled'

_COMMISSION_VARS = ['effective_date', 'scope', 'old_rate', 'new_rate']
_COMMISSION_SAMPLE = {'effective_date': '12 November 2026', 'scope': 'Sedan in Hyderabad',
                      'old_rate': '15.00%', 'new_rate': '18.00%'}

COPY = {
    (COMMISSION, 'in_app'): dict(
        title='Commission change from {effective_date}',
        body='From {effective_date} the platform commission for {scope} changes from '
             '{old_rate} to {new_rate}. It applies to trips that start on or after that date.',
        variables=_COMMISSION_VARS, sample=_COMMISSION_SAMPLE),
    (COMMISSION, 'push'): dict(
        title='Commission change from {effective_date}',
        body='The commission for {scope} changes from {old_rate} to {new_rate}.',
        variables=_COMMISSION_VARS, sample=_COMMISSION_SAMPLE),
    (COMMISSION, 'email'): dict(
        subject='Commission change from {effective_date}',
        body='Hello,\n\nFrom {effective_date} the platform commission for {scope} changes from '
             '{old_rate} to {new_rate}. It applies to trips that start on or after that date.'
             '\n\nYou can review your earnings in the app, or contact driver support.\n\n'
             'SaaradhiGo',
        variables=_COMMISSION_VARS, sample=_COMMISSION_SAMPLE),
    (COMMISSION, 'sms'): dict(
        body='SaaradhiGo: from {effective_date} the commission for {scope} changes from '
             '{old_rate} to {new_rate}. Contact driver support in the app for details.',
        variables=_COMMISSION_VARS, sample=_COMMISSION_SAMPLE),

    (PAYOUT, 'in_app'): dict(
        title='Add your payout details',
        body="We could not include you in this week's payout because no payout details are on "
             'file. Add a UPI ID or bank account in the app to receive your earnings.',
        variables=[], sample={}),
    (PAYOUT, 'push'): dict(
        title='Add your payout details',
        body="You were left out of this week's payout. Add a UPI ID or bank account in the app.",
        variables=[], sample={}),
    (PAYOUT, 'email'): dict(
        subject='Add your payout details',
        body="Hello,\n\nWe could not include you in this week's payout because no payout "
             'details are on file. Add a UPI ID or bank account in the app and your earnings '
             'will be included in the next run.\n\nSaaradhiGo',
        variables=[], sample={}),
    (PAYOUT, 'sms'): dict(
        body="SaaradhiGo: you were left out of this week's payout. Add a UPI ID or bank "
             'account in the app to receive your earnings.',
        variables=[], sample={}),

    (OPERATOR_CANCELLED, 'in_app'): dict(
        title='Your ride was cancelled',
        body='Trip #{trip_id} was cancelled by SaaradhiGo support because it could not '
             'go ahead. You have not been charged. You can book again at any time.',
        variables=['trip_id'], sample={'trip_id': '1042'}),
    (OPERATOR_CANCELLED, 'push'): dict(
        title='Your ride was cancelled',
        body='Trip #{trip_id} was cancelled by support. You have not been charged.',
        variables=['trip_id'], sample={'trip_id': '1042'}),
}

#: Channels each key is seeded on. Anything not listed uses all of CHANNELS.
KEY_CHANNELS = {OPERATOR_CANCELLED: ('in_app', 'push')}

COUNTRIES = ('IN', 'US')
CHANNELS = ('in_app', 'push', 'sms', 'email')


def dlt_text(body):
    """The body as DLT would be asked to approve it: placeholders become {#var#} slots."""
    return templating.PLACEHOLDER.sub(templating.DLT_SLOT, body)


def seed_rows():
    """`[(key, channel, country, fields, active)]` in a stable order."""
    rows = []
    for key in (COMMISSION, PAYOUT, OPERATOR_CANCELLED):
        for country in COUNTRIES:
            for channel in KEY_CHANNELS.get(key, CHANNELS):
                copy = dict(COPY[(key, channel)])
                active = not (channel == 'sms' and country == 'IN')
                rows.append((key, channel, country, copy, active))
    return rows
