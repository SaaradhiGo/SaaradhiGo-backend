"""Masking personal data on the way out.

WHY THIS IS SERVER-SIDE AND NOT A CSS CLASS
-------------------------------------------
The obvious implementation is to send the phone number and let the console
print dots. That is the same mistake as hiding a nav item: the number is in
the JSON, in the browser's network tab, in any log that captures a
response body, and in the memory of every machine the response passed
through. "Masked" would describe the pixels and nothing else.

So an operator without `pii.view` is not SENT the value. There is nothing
to reveal in the client because there is nothing in the payload.

WHAT A MASK HAS TO PRESERVE
---------------------------
A support agent reading a ticket needs to confirm "is this the number the
rider is calling from" without being handed everybody's number. So the mask
keeps the last four digits and the shape, which answers that question and
is useless for bulk extraction. Dropping the field entirely would be
safer still and would make the console unusable for the job it exists for.

An email keeps its first character and its domain for the same reason: it
is enough to recognise an address you were already told, and not enough to
learn one you were not.

THIS IS PHASE 1(e) IN PART
--------------------------
What is here is the masking half. The reveal-and-log half -- a Safety
Officer unmasking one record during an incident, and that reveal landing in
the audit log -- is still to come, and is why `pii.view` is a permission
rather than a property of a role.
"""

#: One bullet, named so f-strings never carry a backslash escape --
#: that is a syntax error before Python 3.12 and this runs on both.
BULLET = '•'

MASK = BULLET * 4  # four bullets: the shape reads as "hidden", not "empty"


def mask_phone(value):
    """`+919876543210` -> `+91••••••3210`.

    Keeps the country prefix and the last four, because those are what let
    a human confirm a number they were already given.
    """
    if not value:
        return value
    text = str(value)
    keep_tail = text[-4:]
    head = ''
    if text.startswith('+'):
        # Keep the country code: a US number and an Indian one must stay
        # distinguishable, or an operator cannot tell which market a
        # record belongs to.
        head = text[:3]
    hidden = max(0, len(text) - len(head) - 4)
    return f'{head}{BULLET * hidden}{keep_tail}'


def mask_email(value):
    """`srinivas@deeptrics.com` -> `s••••••••@deeptrics.com`."""
    if not value or '@' not in str(value):
        return MASK if value else value
    local, _, domain = str(value).partition('@')
    if not local:
        return f'{MASK}@{domain}'
    return f'{local[0]}{BULLET * max(1, len(local) - 1)}@{domain}'


def mask_name(value):
    """`Hyderabad Driver` -> `Hyderabad D.`

    The first name survives. An operator has to be able to say "I am
    looking at Ravi's record" out loud in a stand-up without reading a
    full legal name off the screen.
    """
    if not value:
        return value
    parts = str(value).strip().split()
    if len(parts) == 1:
        return parts[0]
    return f'{parts[0]} {parts[-1][0]}.'


def mask_identifier(value):
    """Mask `username`, whatever it happens to hold.

    THIS FIELD IS WHY THE TESTS SEARCH THE WHOLE BODY.
    --------------------------------------------------
    Accounts here are created with `username=phone_number`, so the number
    appears TWICE in every serialized user. Masking `phone_number` and
    leaving `username` alone passes a field-by-field review and leaks the
    number on every row -- which is what the first run of
    `test_the_number_is_not_in_the_payload_at_all` found.

    It is not always a phone: an operator may have an email there, and a
    handle is not personal data worth mangling. So it is masked by what it
    looks like.
    """
    if not value:
        return value
    text = str(value)
    if '@' in text:
        return mask_email(text)
    if text.startswith('+') or text.replace('-', '').isdigit():
        return mask_phone(text)
    return text


def mask_contact(value):
    """Mask a free-form contact label, whatever it holds.

    SOS rows freeze the raiser as a LABEL (`user_label`), which is their
    full name if they have one and their phone number if they do not; the
    driver on the trip is labelled the same way. So one field may carry
    either kind of personal data, and it is masked by what it looks like:
    a number keeps its country code and last four, a name keeps its first
    name. The frozen label is personal data even after the account is gone.
    """
    if not value:
        return value
    text = str(value).strip()
    if '@' in text:
        return mask_email(text)
    digits = text.replace('-', '').replace(' ', '').lstrip('+')
    if digits.isdigit():
        return mask_phone(text)
    return mask_name(text)


#: Field name -> how to mask it. Driven by name so one table covers every
#: serializer rather than each one deciding for itself, which is how a
#: field ends up masked on one screen and plain on another.
MASKERS = {
    'phone_number': mask_phone,
    # Same digits, second field. See `mask_identifier`.
    'username': mask_identifier,
    'emergency_contact': mask_phone,
    'email': mask_email,
    'full_name': mask_name,
    'dob': lambda v: MASK if v else v,
    'house_no': lambda v: MASK if v else v,
    'street': lambda v: MASK if v else v,
    'zip_code': lambda v: MASK if v else v,
}


def mask_person(data):
    """Mask the personal fields of one serialized person, in place."""
    if not isinstance(data, dict):
        return data
    for field, masker in MASKERS.items():
        if field in data:
            data[field] = masker(data[field])
    return data


def mask_people(payload, keys=('user_details', 'user_id', 'user', 'driver', 'rider')):
    """Walk a serialized payload and mask every nested person.

    Recursive because an admin payload nests people several levels down --
    a trip carries a driver which carries a user -- and a masker that only
    looked at the top level would leave the interesting copy untouched.
    """
    if isinstance(payload, list):
        for item in payload:
            mask_people(item, keys)
        return payload
    if not isinstance(payload, dict):
        return payload

    for key, value in payload.items():
        if key in keys and isinstance(value, dict):
            mask_person(value)
            mask_people(value, keys)
        elif isinstance(value, (dict, list)):
            mask_people(value, keys)
    return payload


def mask_for(user, payload, node=None):
    """Mask `payload` unless this operator may see personal data here.

    One call at the edge of a view. Takes the node so that `pii.view` held
    in Hyderabad does not unmask an Austin record.
    """
    # Through the bridge, NOT `access.can_see_pii` directly.
    #
    # An unmigrated account has no assignments, so the new model grants it
    # nothing and every response to it would come back masked -- changing
    # what today's console shows before a single person has been migrated.
    # `pii.view` is in `LEGACY_OPEN_TO_ANY_OPERATOR` because the endpoints
    # behind it were open to every operator, so the bridge keeps them
    # unmasked until their account is assigned a role. Same rule as scope:
    # migrating the person is what tightens their access.
    from .bridge import granted

    allowed, _how = granted(user, 'pii.view', node)
    if allowed:
        return payload
    return mask_people(payload)
