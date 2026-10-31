"""Notification configuration, read from settings at call time (tests flip settings).

THE SAFETY MODEL IN ONE PLACE
-----------------------------
  1. `NOTIFY_LIVE_SEND` False (the default, everywhere): `effective_provider` answers `stub`
     for every external channel, and the adapters in `providers.live` are never constructed.
  2. `NOTIFY_LIVE_SEND` True outside production: a recipient is sent to only if their phone or
     email is on `NOTIFY_TEST_RECIPIENTS`; anybody else is recorded as
     `suppressed: not on test allowlist`.
  3. `NOTIFY_LIVE_SEND` True in production: the configured provider sends to everyone who has
     not opted out.

Credentials are read by NAME from settings (which read the environment). `credentials_present`
answers yes/no and never returns, logs or formats a value.
"""

from django.conf import settings

#: Credential settings per provider. A provider is "configured" when every one is non-empty.
#: (Names of settings, which are the names of environment variables.)
CREDENTIALS = {
    'msg91': ('MSG91_AUTH_KEY',),
    'twilio': ('TWILIO_ACCOUNT_SID', 'TWILIO_AUTH_TOKEN'),
    'ses': ('SES_AWS_ACCESS_KEY_ID', 'SES_AWS_SECRET_ACCESS_KEY', 'SES_AWS_REGION',
            'SES_FROM_EMAIL'),
    'fcm': ('FIREBASE_CREDENTIALS_JSON',),
    'stub': (),
    'in_app': (),
}

#: Providers that never leave the platform: no cost, no third party, no allowlist.
INTERNAL_PROVIDERS = frozenset({'stub', 'in_app'})

ALLOWLIST_SUPPRESSION = 'not on test allowlist'


def live_send_enabled():
    return bool(getattr(settings, 'NOTIFY_LIVE_SEND', False))


def is_production():
    return bool(getattr(settings, 'IS_PRODUCTION', False))


def normalise(value):
    text = str(value or '').strip()
    if '@' in text:
        return text.lower()
    return ''.join(ch for ch in text if ch.isdigit() or ch == '+')


def test_recipients():
    """The allowlist, normalised: lowercase emails, phones with spaces removed."""
    return {normalise(entry) for entry in (getattr(settings, 'NOTIFY_TEST_RECIPIENTS', None) or [])
            if str(entry).strip()}


def on_allowlist(*addresses):
    allowed = test_recipients()
    return any(a and normalise(a) in allowed for a in addresses)


def configured_provider(channel, country):
    """The provider NAME configured for this channel in this country, or None."""
    table = getattr(settings, 'NOTIFY_PROVIDERS', {}) or {}
    return (table.get(channel) or {}).get(country or '')


def credentials_present(provider):
    """True when every credential setting of `provider` is non-empty. Never the value."""
    names = CREDENTIALS.get(provider)
    if names is None:
        return False
    return all(bool(getattr(settings, name, '')) for name in names)


def effective_provider(channel, country):
    """`(name, live)`: what a send on this channel would use RIGHT NOW.

    `in_app` is internal and always itself. Every other channel is the stub unless the master
    switch is on AND a provider is configured for the channel and country.
    """
    configured = configured_provider(channel, country)
    if configured in INTERNAL_PROVIDERS:
        return configured, False
    if not live_send_enabled() or configured is None:
        return 'stub', False
    return configured, True


def quiet_hours(country):
    table = getattr(settings, 'NOTIFY_QUIET_HOURS', {}) or {}
    return tuple(table.get(country) or getattr(settings, 'NOTIFY_QUIET_HOURS_DEFAULT', (21, 10)))


def max_attempts():
    return max(1, int(getattr(settings, 'NOTIFY_MAX_ATTEMPTS', 5)))


def backoff_seconds(attempts):
    """30 s, 60 s, 120 s, 240 s ... capped at an hour."""
    base = int(getattr(settings, 'NOTIFY_BACKOFF_BASE_SECONDS', 30))
    return min(3600, base * (2 ** max(0, attempts - 1)))


def rate_limit(provider):
    return int((getattr(settings, 'NOTIFY_RATE_LIMITS', {}) or {}).get(provider, 0) or 0)
