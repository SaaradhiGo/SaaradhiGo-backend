"""The integrations status page: one row per provider type per country. READ-ONLY.

WHAT IS AND IS NOT IN A ROW
---------------------------
Whether the provider is configured (an environment variable is PRESENT and non-empty: a boolean),
its mode (`live`, `sandbox`, `stubbed`, `off`), the time of the last success and the last error
where the platform records one, and the go-live blocker it waits on. The NAMES of the environment
variables are shown (they are in the repo); a VALUE, a key prefix or a URL with credentials is
never read into a response. `tests/test_platform_integrations.py` plants a distinctive value in
every credential setting and greps the whole response for it.

Nothing here calls a provider. "Last success" and "last error" come from rows the platform already
writes (notification messages, payments, webhook events); a provider with no such record shows
`null`, not a guess.
"""

from django.conf import settings

from base.countries import COUNTRIES, find_country

PAYMENT_BLOCKER_US = 17

#: Env var NAMES each payment/payout provider needs (names only, never values).
PAYMENT_ENV = {
    'cashfree': ('CASHFREE_APP_ID', 'CASHFREE_SECRET_KEY'),
    'razorpay': ('RAZORPAY_KEY_ID', 'RAZORPAY_KEY_SECRET'),
    'stripe': ('STRIPE_SECRET_KEY',),
}
#: Optional settings whose absence is a WARNING, not "missing" (names only).
PAYMENT_WARN_ENV = {'razorpay': ('RAZORPAY_WEBHOOK_SECRET',)}
PAYOUT_WARN_ENV = {'razorpay': ('RAZORPAYX_WEBHOOK_SECRET',)}
PAYOUT_ENV = {
    'cashfree': ('CASHFREE_PAYOUTS_CLIENT_ID', 'CASHFREE_PAYOUTS_CLIENT_SECRET'),
    # RAZORPAYX_KEY_ID / RAZORPAYX_KEY_SECRET default to the RAZORPAY_* pair when unset.
    'razorpay': ('RAZORPAYX_ACCOUNT_NUMBER', 'RAZORPAY_KEY_ID', 'RAZORPAY_KEY_SECRET'),
    'stripe_connect': ('STRIPE_SECRET_KEY',),
}


def live_providers_required():
    """True when a stubbed provider must BLOCK a launch (production), not just warn (QA)."""
    value = getattr(settings, 'PLATFORM_REQUIRE_LIVE_PROVIDERS', None)
    if value is None:
        return bool(getattr(settings, 'IS_PRODUCTION', False))
    return bool(value)


def _present(*names):
    """True when every named setting is non-empty. Never returns or logs a value."""
    return all(bool(getattr(settings, name, '')) for name in names)


def _iso(value):
    return value.isoformat() if value else None


# ---------------------------------------------------------------------------
# Payments
# ---------------------------------------------------------------------------

def _razorpay_status(kind, currency):
    """Razorpay / RazorpayX row state: presence only, never a value. The mode comes from the
    key PREFIX (test -> sandbox, live -> live), read here without echoing it."""
    from integrations.payments import registry
    from integrations import razorpay_http as rzp

    names = (PAYMENT_ENV if kind == 'payments' else PAYOUT_ENV)['razorpay']
    warn_names = (PAYMENT_WARN_ENV if kind == 'payments' else PAYOUT_WARN_ENV)['razorpay']
    reason = registry.adapter_class(kind, 'razorpay')().not_configured_reason(kind)
    if kind == 'payments':
        key_id = getattr(settings, 'RAZORPAY_KEY_ID', '')
    else:
        key_id = getattr(settings, 'RAZORPAYX_KEY_ID', '') or getattr(settings, 'RAZORPAY_KEY_ID', '')
    configured = reason is None
    mode = ({'test': 'sandbox', 'live': 'live'}.get(rzp.key_mode(key_id)) or 'off') if configured else 'off'
    note = '' if configured else (
        'razorpay credentials are not set.' if not key_id else f'{reason}.')
    warning = ''
    if configured and not _present(*warn_names):
        warning = f'{warn_names[0]} is not set: webhooks are rejected until it is.'
    return {'configured': configured, 'provider': 'razorpay', 'currency': currency, 'mode': mode,
            'blocker': None, 'env': names + warn_names, 'note': note or warning,
            'warning': warning}


def _gateway_status(kind, country_code):
    """The configured provider for a country, read from the SAME registry the call sites use."""
    from integrations.payments import registry
    from servers.payments.payment_gateways.factory import provider_name_for

    country = find_country(country_code)
    currency = country.currency if country else None
    name = provider_name_for(kind, country_code)
    noun = 'payment gateway' if kind == 'payments' else 'payout provider'
    envs = PAYMENT_ENV if kind == 'payments' else PAYOUT_ENV
    if name and registry.known(kind, name):
        state = registry.status(kind, name)
        if state == 'stub':
            return {'configured': False, 'provider': name, 'currency': currency, 'mode': 'off',
                    'blocker': PAYMENT_BLOCKER_US if currency and currency != 'INR' else None,
                    'note': f'{name} is selected for this country but its adapter is a stub '
                            'that is not implemented yet.', 'env': envs.get(name, ())}
        if name == 'razorpay' and currency in registry.CURRENCIES.get(name, ()):
            return _razorpay_status(kind, currency)
        if currency and currency in registry.CURRENCIES.get(name, ()):
            names = envs.get(name, ())
            configured = _present(*names) if names else False
            env = str(getattr(settings, 'CASHFREE_ENVIRONMENT', 'sandbox') or 'sandbox').lower()
            mode = ('live' if env == 'production' else 'sandbox') if configured else 'off'
            return {'configured': configured, 'provider': name, 'currency': currency,
                    'mode': mode, 'blocker': None, 'env': names,
                    'note': '' if configured else f'{name} credentials are not set.'}
        return {'configured': False, 'provider': name, 'currency': currency, 'mode': 'off',
                'blocker': None, 'env': envs.get(name, ()),
                'note': f'{name} cannot settle {currency}.'}
    blocker = PAYMENT_BLOCKER_US if currency and currency != 'INR' else None
    return {'configured': False, 'provider': None, 'currency': currency, 'mode': 'off',
            'blocker': blocker, 'env': (),
            'note': (f'not configured, blocker #17: no {noun} is configured for {country_code} '
                     'until Stripe Connect is confirmed.') if blocker
            else f'no {noun} is configured for this country.'}


def payment_gateway_status(country_code):
    """The payment gateway for a country: `configured`, `provider`, `mode`, `blocker`."""
    return _gateway_status('payments', country_code)


def payout_provider_status(country_code):
    """The payout provider for a country, same shape as `payment_gateway_status`."""
    return _gateway_status('payouts', country_code)


def sms_state(country_code):
    """`{provider, mode}` for notification SMS in a country (mode: live, stubbed, off)."""
    from servers.notifications import config as nconfig

    configured = nconfig.configured_provider('sms', country_code)
    if configured is None:
        return {'provider': None, 'mode': 'off'}
    effective, live = nconfig.effective_provider('sms', country_code)
    if live and nconfig.credentials_present(configured):
        return {'provider': configured, 'mode': 'live'}
    return {'provider': configured, 'mode': 'stubbed'}


# ---------------------------------------------------------------------------
# Last success / last error, from rows the platform already writes
# ---------------------------------------------------------------------------

def _message_times(provider, channel):
    from servers.notifications.models import FAILED, SENT, DELIVERED, NotificationMessage

    base = NotificationMessage.objects.filter(provider=provider, channel=channel)
    ok = (base.filter(status__in=(SENT, DELIVERED), sent_at__isnull=False)
          .order_by('-sent_at').values_list('sent_at', flat=True).first())
    bad = (base.filter(status=FAILED).order_by('-updated_at')
           .values_list('updated_at', flat=True).first())
    return ok, bad


def _payment_times(provider='cashfree'):
    from servers.payments.models import Payment

    ok = (Payment.objects.filter(payment_gateway=provider, status='completed', method='online')
          .order_by('-updated_at').values_list('updated_at', flat=True).first())
    bad = (Payment.objects.filter(payment_gateway=provider, status='failed', method='online')
           .order_by('-updated_at').values_list('updated_at', flat=True).first())
    return ok, bad


# ---------------------------------------------------------------------------
# The table
# ---------------------------------------------------------------------------

def _row(ptype, label, provider, country, configured, mode, *, env=(), blocker=None, note='',
         last_ok=None, last_bad=None):
    return {
        'type': ptype, 'label': label, 'provider': provider, 'country': country,
        'configured': bool(configured), 'mode': mode, 'env_vars': list(env),
        'blocker': blocker, 'note': note,
        'last_success_at': _iso(last_ok), 'last_error_at': _iso(last_bad),
    }


#: Go-live blockers a notification provider waits on, by (channel, provider).
NOTIFY_BLOCKERS = {
    ('sms', 'msg91'): 18, ('sms', 'twilio'): 19, ('whatsapp', 'msg91'): 21,
    ('email', 'ses'): 20, ('push', 'fcm'): 21,
    ('sms', 'sns'): 19, ('push', 'sns'): 21,
}
CHANNEL_LABEL = {'sms': 'SMS', 'whatsapp': 'WhatsApp', 'email': 'Email', 'push': 'Push (Firebase)'}


def rows():
    from servers.notifications import config as nconfig

    out = []
    times = {name: _payment_times(name) for name in ('cashfree', 'razorpay')}
    for code in sorted(COUNTRIES):
        for ptype, label, fn in (('payment_gateway', 'Payment gateway', payment_gateway_status),
                                 ('payout_provider', 'Payout provider', payout_provider_status)):
            state = fn(code)
            provider = state['provider']
            out.append(_row(
                ptype, label, provider or 'none', code, state['configured'],
                state['mode'], env=state['env'] if provider else (),
                blocker=state['blocker'], note=state['note'],
                last_ok=times[provider][0] if (provider in times and ptype == 'payment_gateway') else None,
                last_bad=times[provider][1] if (provider in times and ptype == 'payment_gateway') else None))
    # Notification channels, per country, from the same table the notifications page reads.
    for channel in ('sms', 'whatsapp', 'email', 'push'):
        for code in sorted(COUNTRIES):
            configured = nconfig.configured_provider(channel, code)
            if configured is None:
                if channel in ('sms', 'email', 'push'):
                    out.append(_row(channel, CHANNEL_LABEL[channel], 'none', code, False, 'off',
                                    note='No provider is configured for this country.'))
                continue
            effective, live = nconfig.effective_provider(channel, code)
            present = nconfig.credentials_present(configured)
            mode = 'live' if (live and present) else ('stubbed' if configured != 'in_app' else 'off')
            ok, bad = _message_times(configured, channel)
            note = ''
            if not nconfig.live_send_enabled():
                note = 'Live sending is off (NOTIFY_LIVE_SEND): messages go to the stub.'
            elif not present:
                note = 'Live sending is on but the credentials are not all set.'
            elif nconfig.sns_blocked(configured):
                note = 'AWS SNS is sandbox-only: SNS_LIVE_CONFIRMED is off, so nothing is sent.'
                mode = 'stubbed'
            out.append(_row(
                channel, CHANNEL_LABEL[channel], configured, code, present, mode,
                env=nconfig.CREDENTIALS.get(configured, ()),
                blocker=NOTIFY_BLOCKERS.get((channel, configured)), note=note,
                last_ok=ok, last_bad=bad))
    # The login one-time code (AWS SNS today), the same for every country.
    sns = _present('AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_REGION')
    out.append(_row('otp_sms', 'Login OTP SMS', 'aws_sns', 'all', sns, 'live' if sns else 'off',
                    env=('AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_REGION',
                         'AWS_SNS_SENDER_ID'),
                    blocker=18 if sns else None,
                    note='India needs a DLT-registered sender and template (blocker #18).'))
    maps = _present('GOOGLE_MAPS_API_KEY')
    out.append(_row('maps', 'Maps and places', 'google_maps', 'all', maps,
                    'live' if maps else 'off', env=('GOOGLE_MAPS_API_KEY',)))
    store = _present('AWS_S3_BUCKET_NAME', 'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY')
    out.append(_row('storage', 'File storage', 's3_compatible', 'all', store,
                    'live' if store else 'off',
                    env=('AWS_S3_BUCKET_NAME', 'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY'),
                    blocker=None if store else 4,
                    note='' if store else 'Document upload needs the R2 credentials (blocker #4).'))
    sentry = _present('SENTRY_DSN')
    out.append(_row('error_tracking', 'Error tracking', 'sentry', 'all', sentry,
                    'live' if sentry else 'off', env=('SENTRY_DSN',)))
    return out


def status_page():
    from servers.notifications import config as nconfig

    return {
        'rows': rows(),
        'live_send': nconfig.live_send_enabled(),
        'production': bool(getattr(settings, 'IS_PRODUCTION', False)),
        'live_providers_required': live_providers_required(),
        'note': 'Presence of a setting only: no value, key prefix or credentialed URL is ever '
                'returned.',
    }
