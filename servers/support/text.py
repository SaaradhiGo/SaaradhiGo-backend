"""Free text leaves the admin API with contact details masked.

A rider writes "call me on +91 98765 43210" in a ticket. The thread is shown
to operators who may not hold `pii.view`, so phone numbers and emails inside
free text are masked with the same maskers the structured fields use. The
whole thread is available through the logged reveal (`support_ticket` target,
field `thread`).
"""

import re

from servers.admin_access.masking import mask_email, mask_phone

_EMAIL = re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}')
# A run of digits with the usual separators, at least ten digits in all.
_PHONE = re.compile(r'(?<![\w•])\+?\d[\d\s().-]{7,}\d(?![\w•])')


def _phone(match):
    text = match.group(0)
    digits = re.sub(r'\D', '', text)
    if len(digits) < 10:
        return text
    return mask_phone(('+' if text.startswith('+') else '') + digits)


def mask_free_text(value):
    """`value` with phone numbers and email addresses masked."""
    if not value:
        return value
    text = _EMAIL.sub(lambda m: mask_email(m.group(0)), str(value))
    return _PHONE.sub(_phone, text)
