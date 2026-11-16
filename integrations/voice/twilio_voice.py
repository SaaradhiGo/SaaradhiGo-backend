"""Twilio Voice: masked calling and SOS paging in the USA (13 Nov), on the same Twilio account as SMS.

  POST https://api.twilio.com/2010-04-01/Accounts/<sid>/Calls.json  (basic auth sid, token)
       To, From (TWILIO_VOICE_NUMBER), Twiml

Masked call (click-to-call): Twilio rings the caller from our number, then <Dial>s the other party
with our number as caller ID. Neither side sees the other's number. The call is cut at the end
of the trip's call window. Paging (SOS escalation): Twilio rings the on-call phone and reads the
message twice. Numbers are never logged.

India has no voice provider yet (MSG91 SMS does not cover voice): CALL_BRIDGE_PROVIDERS maps only
"US" to this adapter, and India stays on the fake until Srini chooses one.
"""

import logging
from xml.sax.saxutils import escape, quoteattr

import requests
from django.conf import settings
from django.utils import timezone

from servers.callbridge.providers import Bridge, CallingUnavailable

logger = logging.getLogger(__name__)
TIMEOUT = 10


class TwilioVoice:
    name = 'twilio'

    def __init__(self):
        self.sid = getattr(settings, 'TWILIO_ACCOUNT_SID', '') or ''
        self.token = getattr(settings, 'TWILIO_AUTH_TOKEN', '') or ''
        self.number = getattr(settings, 'TWILIO_VOICE_NUMBER', '') or ''
        if not (self.sid and self.token and self.number):
            raise CallingUnavailable('twilio voice is not configured')

    def _post(self, path, data):
        url = f'https://api.twilio.com/2010-04-01/Accounts/{self.sid}/{path}'
        try:
            r = requests.post(url, data=data, auth=(self.sid, self.token), timeout=TIMEOUT)
        except requests.RequestException as exc:
            raise CallingUnavailable(f'twilio request failed: {type(exc).__name__}') from None
        if r.status_code >= 400:
            raise CallingUnavailable(f'twilio answered HTTP {r.status_code}')
        return r.json()

    def start(self, session, caller_phone, callee_phone):
        seconds = max(60, int((session.expires_at - timezone.now()).total_seconds()))
        twiml = ('<Response><Say>Connecting your SaaradhiGo call.</Say>'
                 f'<Dial callerId={quoteattr(self.number)} timeLimit="{seconds}">'
                 f'<Number>{escape(callee_phone)}</Number></Dial></Response>')
        data = self._post('Calls.json', {'To': caller_phone, 'From': self.number, 'Twiml': twiml})
        logger.info('call_bridge_twilio_start session=%s', session.pk)
        return Bridge(kind='session', number='', ref=str(data.get('sid') or ''))

    def end(self, session):
        if session.provider_ref:
            try:
                self._post(f'Calls/{session.provider_ref}.json', {'Status': 'completed'})
            except CallingUnavailable:
                logger.warning('call_bridge_twilio_end failed session=%s', session.pk)

    def page(self, phone, message, purpose):
        twiml = f'<Response><Say loop="2">{escape(message)}</Say></Response>'
        data = self._post('Calls.json', {'To': phone, 'From': self.number, 'Twiml': twiml})
        logger.info('call_bridge_twilio_page purpose=%s', purpose)
        return str(data.get('sid') or '')
