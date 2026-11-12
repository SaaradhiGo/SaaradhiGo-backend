"""Call bridge providers. Business code asks `get_provider()` and never sees a vendor.

    CALL_BRIDGE_PROVIDER    fake (default) | the name of a real adapter in _REAL
    CALL_BRIDGE_REAL_ENABLED  False (default). A real adapter is built only when this is on.

THE FAKE PROVIDER (QA) bridges nothing: it logs the session and returns CALL_BRIDGE_FAKE_NUMBER,
a test number that belongs to neither party. No real adapter exists yet; adding one is a module
under integrations/ (a masked-number / virtual-number API such as Exotel, Knowlarity, Plivo or
Twilio Proxy) implementing `start` and `end`, and one line in _REAL.
"""

import logging
import uuid
from dataclasses import dataclass

from django.conf import settings

logger = logging.getLogger(__name__)


class CallingUnavailable(Exception):
    """No provider may place this call (the real one is configured but switched off)."""


@dataclass
class Bridge:
    #: number: dial `number` (a temporary proxy number). session: the provider rings both
    #: phones itself (click-to-call) and `number` is empty.
    kind: str
    number: str
    ref: str


class FakeProvider:
    name = 'fake'

    def start(self, session, caller_phone, callee_phone):
        # Ids only. The numbers are accepted to keep the real signature and are never logged.
        logger.info('call_bridge_fake_start session=%s trip=%s from=%s',
                    session.pk, session.trip_id, session.from_role)
        return Bridge(kind='number', number=settings.CALL_BRIDGE_FAKE_NUMBER,
                      ref=f'fake-{uuid.uuid4().hex[:12]}')

    def end(self, session):
        logger.info('call_bridge_fake_end session=%s', session.pk)

    def page(self, phone, message, purpose):
        """Ring one staff phone and read `message` (12 Nov, SOS escalation). Returns a reference.
        The fake rings nothing and logs no number."""
        ref = f'fake-{uuid.uuid4().hex[:12]}'
        logger.info('call_bridge_fake_page purpose=%s ref=%s', purpose, ref)
        return ref


#: name -> "module:Class" of real adapters. Twilio Voice for the USA (13 Nov); India: none chosen.
_REAL = {'twilio': 'integrations.voice.twilio_voice:TwilioVoice'}


def provider_name(country=None):
    """CALL_BRIDGE_PROVIDERS ({"US": "twilio"}) for the market, else CALL_BRIDGE_PROVIDER."""
    table = getattr(settings, 'CALL_BRIDGE_PROVIDERS', None) or {}
    name = table.get(str(country or '').upper()) if country else None
    return (name or getattr(settings, 'CALL_BRIDGE_PROVIDER', 'fake') or 'fake').strip().lower()


def country_of_phone(phone):
    return 'US' if str(phone or '').startswith('+1') else 'IN'


def get_provider(country=None):
    name = provider_name(country)
    if name == 'fake':
        return FakeProvider()
    if not getattr(settings, 'CALL_BRIDGE_REAL_ENABLED', False) or name not in _REAL:
        raise CallingUnavailable(name)
    import importlib

    module, cls = _REAL[name].split(':')
    return getattr(importlib.import_module(module), cls)()
