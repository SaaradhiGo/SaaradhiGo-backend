"""Provider adapters, selected by settings per channel x country.

`build(name, channel)` is the ONLY place a live adapter is constructed, and it refuses unless
`NOTIFY_LIVE_SEND` is on. The stub and in-app providers are always available.
"""

from .. import config
from .base import LiveSendDisabled, Provider  # noqa: F401
from .internal import InAppProvider, StubProvider

#: (provider, channel) -> "module:Class", imported only when asked for. The adapters live in the
#: top-level `integrations/` package (the vendor boundary); this table is the only place the
#: notifications app names them.
_LIVE = {
    ('msg91', 'sms'): ('integrations.sms.msg91', 'Msg91Sms'),
    ('msg91', 'whatsapp'): ('integrations.sms.msg91', 'Msg91WhatsApp'),
    ('twilio', 'sms'): ('integrations.sms.twilio', 'TwilioSms'),
    ('sns', 'sms'): ('integrations.sms.sns', 'SnsSms'),
    ('ses', 'email'): ('integrations.email.ses', 'SesEmail'),
    ('fcm', 'push'): ('integrations.push.fcm', 'FcmPush'),
    ('sns', 'push'): ('integrations.push.sns', 'SnsPush'),
}


def live_class(name, channel):
    import importlib

    try:
        module, cls = _LIVE[(name, channel)]
    except KeyError:
        raise LookupError(f'no live adapter for provider {name!r} on channel {channel!r}') from None
    return getattr(importlib.import_module(module), cls)


def build(name, channel):
    """The adapter instance for `name`. Live adapters need the master switch."""
    if name == 'stub':
        return StubProvider()
    if name == 'in_app':
        return InAppProvider()
    if not config.live_send_enabled():
        raise LiveSendDisabled(
            f'refusing to construct the live {name!r} adapter: NOTIFY_LIVE_SEND is off')
    return live_class(name, channel)()


def webhook_class(provider):
    """The adapter class that owns a webhook route (static methods only; never constructed)."""
    return {
        'twilio': live_class('twilio', 'sms'),
        'ses': live_class('ses', 'email'),
        'msg91': live_class('msg91', 'sms'),
    }.get(provider)
