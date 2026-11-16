"""Exotel Voice: masked rider-driver calls and SOS escalation calls in India (launch round 3).

Checked against developer.exotel.com (make-a-call-api, connect-to-flow) on 16 Nov 2026:

  POST https://<EXOTEL_SUBDOMAIN>/v1/Accounts/<sid>/Calls/connect.json   (basic auth key, token)

Masked call: Exotel rings `From` (the caller) first and, once answered, dials `To`, both seeing
the ExoPhone (`CallerId`). `TimeLimit` cuts the call at the end of the trip's call window: Exotel
documents no "end this call" request, so `end` only logs. SOS paging: Exotel rings the on-call
phone into a call flow (EXOTEL_SOS_FLOW_ID, built in Exotel's App Bazaar) that says the SOS
message; the API cannot pass spoken text, so the flow's words are fixed and `CustomField` carries
the purpose. Numbers are never logged. Mumbai-cluster accounts use api.in.exotel.com.
"""

import logging

import requests
from django.conf import settings
from django.utils import timezone

from servers.callbridge.providers import Bridge, CallingUnavailable

logger = logging.getLogger(__name__)
TIMEOUT = 10
MAX_SECONDS = 14400  # Exotel's TimeLimit ceiling


class ExotelVoice:
    name = 'exotel'

    def __init__(self):
        self.sid = getattr(settings, 'EXOTEL_ACCOUNT_SID', '') or ''
        self.key = getattr(settings, 'EXOTEL_API_KEY', '') or ''
        self.token = getattr(settings, 'EXOTEL_API_TOKEN', '') or ''
        self.caller_id = getattr(settings, 'EXOTEL_CALLER_ID', '') or ''
        self.host = (getattr(settings, 'EXOTEL_SUBDOMAIN', '') or 'api.exotel.com').strip().strip('/')
        if not (self.sid and self.key and self.token and self.caller_id):
            raise CallingUnavailable('exotel voice is not configured')

    def _connect(self, data):
        url = f'https://{self.host}/v1/Accounts/{self.sid}/Calls/connect.json'
        try:
            r = requests.post(url, data=data, auth=(self.key, self.token), timeout=TIMEOUT)
        except requests.RequestException as exc:
            raise CallingUnavailable(f'exotel request failed: {type(exc).__name__}') from None
        if r.status_code >= 400:
            raise CallingUnavailable(f'exotel answered HTTP {r.status_code}')
        try:
            return str(((r.json() or {}).get('Call') or {}).get('Sid') or '')
        except ValueError:
            return ''

    def start(self, session, caller_phone, callee_phone):
        seconds = max(60, int((session.expires_at - timezone.now()).total_seconds()))
        ref = self._connect({'From': caller_phone, 'To': callee_phone, 'CallerId': self.caller_id,
                             'CallType': 'trans', 'TimeLimit': str(min(seconds, MAX_SECONDS)),
                             'TimeOut': '30'})
        logger.info('call_bridge_exotel_start session=%s', session.pk)
        return Bridge(kind='session', number='', ref=ref)

    def end(self, session):
        # No documented end-call request: TimeLimit (set in start) closes the call window.
        logger.info('call_bridge_exotel_end session=%s (closed by TimeLimit)', session.pk)

    def page(self, phone, message, purpose):
        flow = getattr(settings, 'EXOTEL_SOS_FLOW_ID', '') or ''
        if not flow:
            raise CallingUnavailable('exotel SOS flow is not configured')
        ref = self._connect({'From': phone, 'CallerId': self.caller_id, 'CallType': 'trans',
                             'Url': f'http://my.exotel.com/{self.sid}/exoml/start_voice/{flow}',
                             'CustomField': str(purpose)[:128], 'TimeLimit': '300', 'TimeOut': '45'})
        logger.info('call_bridge_exotel_page purpose=%s', purpose)
        return ref
