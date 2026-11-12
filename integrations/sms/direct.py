"""Direct SMS by market (13 Nov): sign-in codes, trip shares, SOS alerts and on-call pages.

Srini's ruling: India sends through MSG91 (DLT), the USA through Twilio. `SMS_DIRECT_PROVIDERS`
(JSON, e.g. {"IN": "msg91", "US": "twilio"}) picks the provider per number's country; a country
not listed keeps AWS SNS, exactly as before, so nothing changes until Srini sets it.

MSG91 needs, per message kind (base/sms_templates.py): the DLT template id
(`MSG91_DLT_TEMPLATE_IDS`) and the MSG91 flow id that carries it (`MSG91_FLOW_IDS`); the flow holds
the sender id (`MSG91_SENDER_ID`) and the DLT entity (`MSG91_DLT_ENTITY_ID`). A text that does not
match its registered template exactly is refused here, before MSG91 or an operator drops it.
"""

import logging
from types import SimpleNamespace

from django.conf import settings

from base import sms_templates
from servers.notifications.providers.base import PermanentProviderError

logger = logging.getLogger(__name__)


def provider_for(phone):
    table = getattr(settings, 'SMS_DIRECT_PROVIDERS', None) or {}
    return (table.get(sms_templates.country_of_phone(phone)) or '').strip().lower()


def send(phone, text, kind='', values=None):
    """Send through the market's provider. Returns a message id. Raises the provider's
    PermanentProviderError (do not retry) or TransientProviderError (retry)."""
    provider = provider_for(phone)
    if provider == 'msg91':
        return _msg91(phone, text, kind, values or {})
    if provider == 'twilio':
        from integrations.sms.twilio import TwilioSms

        result = TwilioSms().send(SimpleNamespace(to_phone=phone, body=text))
        return result.provider_message_id
    raise LookupError(provider)


def _msg91(phone, text, kind, values):
    from integrations.sms import msg91

    if not kind or kind not in sms_templates.TEMPLATES:
        raise PermanentProviderError('MSG91: this message has no registered DLT template kind')
    if not sms_templates.matches(kind, text):
        raise PermanentProviderError(f'MSG91: the text does not match DLT template "{kind}"')
    flow = (getattr(settings, 'MSG91_FLOW_IDS', None) or {}).get(kind)
    dlt = (getattr(settings, 'MSG91_DLT_TEMPLATE_IDS', None) or {}).get(kind)
    if not (flow and dlt and getattr(settings, 'MSG91_AUTH_KEY', '')):
        raise PermanentProviderError(f'MSG91: flow id, DLT template id or auth key missing for "{kind}"')
    recipient = {'mobiles': ''.join(ch for ch in phone if ch.isdigit())}
    recipient.update({name: str(values.get(name, '')) for name in sms_templates.variables(kind)})
    data = msg91._post(msg91.SMS_URL, {'template_id': flow, 'short_url': '0', 'recipients': [recipient]})
    return str(data.get('message') or '')
