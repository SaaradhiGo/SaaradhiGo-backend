"""Every SMS this platform sends outside the notifications pipeline, in one place (13 Nov).

India requires each SMS to match a DLT-registered template EXACTLY (TRAI TCCCPR 2018): the fixed
text character for character, variables only where the template has `{#var#}`. So the text is
never written at the call site: callers ask `render(kind, **values)`, and the same entry produces
the DLT text for registration (`dlt_text`) and the CSV Srini submits (`export_dlt_templates`).

The notifications pipeline's SMS templates (seed_data.py) carry their own DLT text on
`TemplateRegistration.registered_text`; the export lists both.

Category, in DLT terms: `service` (service-implicit: one-time codes and trip messages a user's
own action causes; non-bank OTPs are service-implicit, the "transactional" category is for banks)
or `promotional`. Changing a text means registering a new DLT template first.
"""

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class SmsTemplate:
    kind: str
    text: str                    # Python format string: {name} is a variable
    category: str                # 'service' | 'promotional'
    purpose: str
    sample: dict = field(default_factory=dict)
    url_vars: tuple = ()         # variables that carry a link ({#url#} in DLT)


TEMPLATES = {t.kind: t for t in (
    SmsTemplate('otp', 'Your OTP for SaaradhiGo is {otp}. It will expire in 10 minutes.',
                'service', 'Sign-in one-time code', {'otp': '482913'}),
    SmsTemplate('trip_share', '{name} is on a SaaradhiGo trip. Follow it live: {url}',
                'service', "A rider's trip shared with their trusted contacts",
                {'name': 'Asha', 'url': 'https://www.saaradhigo.com/t/Ab12Cd34'}, ('url',)),
    SmsTemplate('sos_contact', '{name} pressed SOS during a SaaradhiGo trip. Track the trip: {url}. '
                'If you think they are in danger, call the local emergency number.',
                'service', 'SOS alert to a rider\'s trusted contacts',
                {'name': 'Asha', 'url': 'https://www.saaradhigo.com/t/Ab12Cd34'}, ('url',)),
    SmsTemplate('contact_added', '{name} added you as a trusted contact on SaaradhiGo. Reply STOP to opt out.',
                'service', 'Telling a person they were added as a trusted contact', {'name': 'Asha'}),
    SmsTemplate('sos_emergency', 'SaaradhiGo SOS: {name} has raised an SOS. Please call them. SOS#{sos_id}.',
                'service', 'SOS alert to the emergency contact on the account',
                {'name': 'Asha Rao', 'sos_id': '1042'}),
    SmsTemplate('oncall_page', 'SaaradhiGo SOS {ref} has not been acknowledged. Open the console now.',
                'service', 'SOS escalation to on-call staff when a call cannot be placed',
                {'ref': 'SOS-1042'}),
)}

_VAR = re.compile(r'\{(\w+)\}')


def render(kind, **values):
    """The exact text for `kind`. Raises KeyError for an unknown kind or a missing value."""
    return TEMPLATES[kind].text.format(**values)


def variables(kind):
    return _VAR.findall(TEMPLATES[kind].text)


def dlt_text(kind):
    """The text as registered on DLT: each variable becomes {#var#} (a link {#url#})."""
    t = TEMPLATES[kind]
    return _VAR.sub(lambda m: '{#url#}' if m.group(1) in t.url_vars else '{#var#}', t.text)


def matches(kind, text):
    """True when `text` is exactly this template with some values in the variable slots."""
    parts = _VAR.split(TEMPLATES[kind].text)
    pattern = ''.join(re.escape(p) if i % 2 == 0 else r'(.{1,500}?)' for i, p in enumerate(parts))
    return re.fullmatch(pattern, text or '', flags=re.S) is not None


def country_of_phone(phone):
    """'IN' for +91, 'US' for +1, else ''."""
    phone = str(phone or '')
    if phone.startswith('+91'):
        return 'IN'
    if phone.startswith('+1'):
        return 'US'
    return ''
