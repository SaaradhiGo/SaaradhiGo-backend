"""Masking personal data on the way out.

WHY THIS IS SERVER-SIDE AND NOT A CSS CLASS
-------------------------------------------
The obvious implementation is to send the phone number and let the console
print dots. That is the same mistake as hiding a nav item: the number is in
the JSON, in the browser's network tab, in any log that captures a
response body, and in the memory of every machine the response passed
through. "Masked" would describe the pixels and nothing else.

So an operator is not SENT the value. There is nothing to reveal in the
client because there is nothing in the payload. The one way to a real value
is a REVEAL (`admin_access.reveal`, or the SOS incident reveal), which
hands over the fields that were asked for, for one record, and writes an
audit row naming the operator, the record and the fields.

THE RULE, PER OPERATOR (owner's ruling, Phase 1(e))
---------------------------------------------------
Masked BY DEFAULT for EVERY operator, Super Admin included. `pii.view` is
not "sees everything"; it is the capability to reveal, one record at a
time, logged. Concretely, in every list and detail response:

  * phone numbers, emails, usernames, emergency contacts: masked
    (country code + last four; first letter + domain);
  * street address, house number, postcode, date of birth: masked or
    withheld; a free-text address keeps its locality and city;
  * payout identifiers (UPI handle, bank account, IFSC): masked to a
    recognisable tail;
  * identity documents and photos (licence, RC, insurance, avatar, receipt
    PDF): withheld unless the caller holds `pii.view`, because reading them
    IS the KYC Verifier's job and a link that expires in fifteen minutes is
    not something a reveal call per document would make safer;
  * coordinates: exact for `pii.view` holders (the live map), rounded to
    two decimals (~1 km) for everybody else;
  * NAMES: full for `pii.view` holders -- an operator working a queue has
    to recognise a person -- and first name + initial for everybody else,
    which includes the Analyst, who sees no personal data anywhere.

Two policies, then: `STRICT` (no `pii.view`) and `TRUSTED` (`pii.view`
held). Both mask contact details and identifiers; they differ only in
names, coordinate precision and documents.

WHERE IT IS APPLIED
-------------------
Once, at the edge: `servers.admin_access.rendering.MaskingJSONRenderer` is
the default DRF renderer and masks every response produced under the
operator namespace (`/api/v1/**/admin/**`, `/api/v1/admin-access/**`, or
any view gated on `ScopedPermission`) by FIELD NAME, recursively. A view
does not opt in, so a new endpoint cannot leak by default; the only way
out is `mark_revealed(request)`, which only the reveal views call. Views
may still call `mask_for` themselves (the maskers are idempotent), but
nothing depends on them remembering to.

WHAT A MASK HAS TO PRESERVE
---------------------------
A support agent reading a ticket needs to confirm "is this the number the
rider is calling from" without being handed everybody's number. So the mask
keeps the last four digits and the shape, which answers that question and
is useless for bulk extraction. Dropping the field entirely would be
safer still and would make the console unusable for the job it exists for.
"""

import logging
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

logger = logging.getLogger(__name__)

#: One bullet, named so f-strings never carry a backslash escape --
#: that is a syntax error before Python 3.12 and this runs on both.
BULLET = '•'

MASK = BULLET * 4  # four bullets: the shape reads as "hidden", not "empty"

#: The audit action written by the generic reveal. Grep for it.
REVEAL_ACTION = 'pii_revealed'

#: The audit action written when an export is handed over unmasked.
EXPORT_REVEAL_ACTION = 'pii_export_revealed'

#: Coordinates are rounded to this many decimals for `STRICT` viewers.
#: Two decimals of latitude is about 1.1 km, which places a trip in a
#: neighbourhood and not at a front door.
COORDINATE_DECIMALS = 2


# ---------------------------------------------------------------------------
# The maskers. Every one is idempotent: masking a masked value returns it
# unchanged, so a view that masked its own payload and the renderer that
# masks it again agree.
# ---------------------------------------------------------------------------

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


def _contact_shaped(text):
    """True for an email or a phone number, whichever field it sits in."""
    return '@' in text or text.replace('-', '').replace(' ', '').lstrip('+').isdigit()


def mask_name(value):
    """`Ravi Kumar` -> `Ravi K.`

    The first name survives. An operator has to be able to say "I am
    looking at Ravi's record" out loud in a stand-up without reading a
    full legal name off the screen.

    A "name" that is really a phone number or an email (an account created
    without a name shows its number there) is masked as what it is: a
    single word survives whole, so `+919876543210` would otherwise leak.
    """
    if not value:
        return value
    text = str(value).strip()
    if _contact_shaped(text):
        return mask_contact(text)
    parts = text.split()
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


def mask_last4(value):
    """`123456789012` -> `••••••••9012`; `HDFC0001234` -> `•••••••1234`.

    For bank accounts, IFSC codes and licence numbers: enough to match a
    statement line, useless to move money with.
    """
    if not value:
        return value
    text = str(value)
    if len(text) <= 4:
        return MASK
    return f'{BULLET * (len(text) - 4)}{text[-4:]}'


def mask_upi(value):
    """`ravi.kumar@okaxis` -> `r•••••••••@okaxis`.

    The handle is a payment address AND usually a name or a phone number,
    so the local part goes and the bank suffix stays: Finance can see which
    rail a payout is on without being handed the address.
    """
    if not value:
        return value
    text = str(value)
    if '@' in text:
        return mask_email(text)
    return mask_last4(text)


def mask_address(value):
    """`Plot 12, Road No 3, Banjara Hills, Hyderabad, Telangana 500034`
    -> `•••• Banjara Hills, Hyderabad, Telangana 500034`.

    Keeps the locality and city, drops the house and the street. Addresses
    arrive as comma-separated components with the most specific first,
    so the rule is positional: the last three components survive when
    there are four or more, the last two of three, the last of two. A
    single component cannot be told apart from a house number and is
    withheld. Postcodes (any run of five or more digits) are dropped from
    what survives: a PIN narrows to a few streets, and it is the same
    string as the blanked `zip_code` field. The bullet prefix says "there
    was more"; it is also how a second pass knows the value is masked.
    """
    if not value:
        return value
    text = str(value).strip()
    if text.startswith(BULLET):
        return text
    parts = [p.strip() for p in text.split(',') if p.strip()]
    if len(parts) >= 4:
        kept = parts[-3:]
    elif len(parts) == 3:
        kept = parts[-2:]
    elif len(parts) == 2:
        kept = parts[-1:]
    else:
        return MASK
    kept = [_POSTCODE.sub('', part).strip() for part in kept]
    return f'{MASK} {", ".join(p for p in kept if p)}'


_POSTCODE = re.compile(r'\b\d{5,}\b')


def round_coordinate(value, decimals=COORDINATE_DECIMALS):
    """`17.4123456` -> `17.41`, in the type it arrived in.

    Serializers hand coordinates over as strings (DRF renders Decimals as
    strings), the live map as floats, hand-built payloads as either.
    """
    if value is None or value == '':
        return value
    try:
        as_decimal = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return value
    quantum = Decimal(1).scaleb(-decimals)
    rounded = as_decimal.quantize(quantum)
    if isinstance(value, float):
        return float(rounded)
    if isinstance(value, Decimal):
        return rounded
    return str(rounded)


def _blank(value):
    return MASK if value not in (None, '') else value


def _withhold(value):
    return None if value else value


# ---------------------------------------------------------------------------
# Field name -> kind. Driven by name so ONE table covers every serializer
# and every hand-built payload, rather than each one deciding for itself --
# which is how a field ends up masked on one screen and plain on another.
# ---------------------------------------------------------------------------

PHONE_FIELDS = frozenset({
    'phone_number', 'phone', 'mobile', 'driver_phone', 'rider_phone',
    'user_phone', 'requester_phone', 'payer_phone', 'customer_phone',
    'emergency_contact',
})
EMAIL_FIELDS = frozenset({
    'email', 'driver_email', 'rider_email', 'user_email', 'requester_email',
    'payer_email', 'customer_email', 'sent_to_email',
})
IDENTIFIER_FIELDS = frozenset({'username'})
CONTACT_LABEL_FIELDS = frozenset({'user_label', 'driver_label'})
NAME_FIELDS = frozenset({
    'full_name', 'driver_name', 'rider_name', 'user', 'user_name', 'author_name',
    'rater_name', 'requester_name', 'payer_name', 'customer_name',
})
ADDRESS_FIELDS = frozenset({
    'pickup_address', 'destination_address', 'drop_address', 'address',
    'address_text',
})
BLANKED_FIELDS = frozenset({
    'dob', 'date_of_birth', 'house_no', 'street', 'zip_code', 'postcode',
    'aadhaar', 'aadhaar_number', 'pan', 'pan_number', 'ssn',
})
LAST4_FIELDS = frozenset({
    'account_number', 'bank_account_number', 'ifsc', 'ifsc_code',
    'license_number', 'licence_number',
})
UPI_FIELDS = frozenset({'upi_id', 'driver_upi_id', 'vpa'})
COORDINATE_FIELDS = frozenset({
    'lat', 'lng', 'lon', 'latitude', 'longitude',
    'pickup_lat', 'pickup_long', 'pickup_lng', 'destination_lat',
    'destination_long', 'destination_lng',
})
DOCUMENT_FIELDS = frozenset({
    'license_doc', 'license_doc_back', 'license_doc_url', 'license_doc_back_url',
    'rc_doc', 'rc_doc_url', 'permit_doc', 'permit_doc_url',
    'insurance_doc', 'insurance_doc_url', 'fitness_doc', 'fitness_doc_url',
    'puc_doc', 'puc_doc_url', 'inspection_doc', 'inspection_doc_url',
    'vehicle_pic', 'vehicle_pic_url', 'avatar', 'avatar_url', 'pdf_url',
})

#: Containers that hold a STAFF ATTRIBUTION SNAPSHOT -- who made, checked or
#: created a row (`{id, email, label, role}` as the maker-checker and
#: policy rows carry it). An operator's name on a decision is the audit
#: trail, not customer data: a checker who cannot see which colleague
#: raised a payout cannot do the check. A dict under one of these keys is
#: left alone; anything else nested anywhere is masked as usual.
OPERATOR_IDENTITY_KEYS = frozenset({'maker', 'checker', 'created_by'})

#: Every field name this module has an opinion about. The sweep guard test
#: checks every admin response against this table.
ALL_PII_FIELDS = (
    PHONE_FIELDS | EMAIL_FIELDS | IDENTIFIER_FIELDS | CONTACT_LABEL_FIELDS
    | NAME_FIELDS | ADDRESS_FIELDS | BLANKED_FIELDS | LAST4_FIELDS
    | UPI_FIELDS | COORDINATE_FIELDS | DOCUMENT_FIELDS
)

#: The maskers that apply whatever the policy: contact details and
#: identifiers are masked for everybody, Super Admin included.
ALWAYS_MASKED = {}
ALWAYS_MASKED.update(dict.fromkeys(PHONE_FIELDS, mask_phone))
ALWAYS_MASKED.update(dict.fromkeys(EMAIL_FIELDS, mask_email))
ALWAYS_MASKED.update(dict.fromkeys(IDENTIFIER_FIELDS, mask_identifier))
ALWAYS_MASKED.update(dict.fromkeys(CONTACT_LABEL_FIELDS, mask_contact))
ALWAYS_MASKED.update(dict.fromkeys(ADDRESS_FIELDS, mask_address))
ALWAYS_MASKED.update(dict.fromkeys(BLANKED_FIELDS, _blank))
ALWAYS_MASKED.update(dict.fromkeys(LAST4_FIELDS, mask_last4))
ALWAYS_MASKED.update(dict.fromkeys(UPI_FIELDS, mask_upi))

#: Kept for callers that import the old table name. Same rules.
MASKERS = ALWAYS_MASKED


# ---------------------------------------------------------------------------
# Policy: what `pii.view` changes, and what it does not
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Policy:
    """What a viewer gets beyond the always-masked baseline."""

    full_names: bool
    exact_coordinates: bool
    documents: bool
    label: str


#: No `pii.view`: the Analyst, the Support Agent, Marketing, and any
#: non-operator who somehow reaches an operator response.
STRICT = Policy(full_names=False, exact_coordinates=False, documents=False,
                label='strict')

#: `pii.view` held (somewhere): names and the live map, documents for KYC.
#: Contact details and identifiers are STILL masked; reveal is the way in.
TRUSTED = Policy(full_names=True, exact_coordinates=True, documents=True,
                 label='trusted')


def policy_for(user, node=None):
    """The policy for this operator, at `node` if one is given.

    Through the bridge, NOT `access.can_see_pii` directly: an unmigrated
    account holds `pii.view` through `LEGACY_OPEN_TO_ANY_OPERATOR` and so
    gets `TRUSTED` -- which still masks every phone and email. Masking is
    not a migration question; what the bridge decides is only whether the
    account sees full names, exact coordinates and documents.

    Without a node the question is "holds it anywhere", which is the right
    question for a list that is already filtered to the caller's scope.
    """
    from .bridge import granted

    allowed, _how = granted(user, 'pii.view', node)
    return TRUSTED if allowed else STRICT


def mask_payload(payload, policy=STRICT):
    """Mask every personal field in `payload`, recursively, in place.

    Walks dicts and lists at any depth, because an admin payload nests
    people several levels down -- a trip carries a driver which carries a
    user -- and a masker that only looked at the top level would leave the
    interesting copy untouched. Decided by KEY NAME: the table above is the
    whole policy, and a value under an unlisted key is left alone.
    """
    if isinstance(payload, list):
        for item in payload:
            if isinstance(item, (dict, list)):
                mask_payload(item, policy)
        return payload
    if not isinstance(payload, dict):
        return payload

    for key, value in list(payload.items()):
        if key in OPERATOR_IDENTITY_KEYS and isinstance(value, dict):
            continue
        if isinstance(value, (dict, list)):
            mask_payload(value, policy)
            continue
        masker = ALWAYS_MASKED.get(key)
        if masker is not None:
            payload[key] = masker(value)
        elif key in NAME_FIELDS:
            if not policy.full_names and isinstance(value, str):
                payload[key] = mask_name(value)
            elif isinstance(value, str) and _contact_shaped(value.strip()):
                # A full name is shown to trusted viewers; a number in the
                # name field is still a number, masked for everybody.
                payload[key] = mask_contact(value)
        elif key in COORDINATE_FIELDS:
            if not policy.exact_coordinates:
                payload[key] = round_coordinate(value)
        elif key in DOCUMENT_FIELDS:
            if not policy.documents:
                payload[key] = _withhold(value)
    return payload


def mask_person(data, policy=STRICT):
    """Mask the personal fields of one serialized person, in place."""
    return mask_payload(data, policy)


def mask_people(payload, keys=None, policy=STRICT):
    """Walk a serialized payload and mask every nested person.

    `keys` is accepted for callers written against the earlier signature
    and ignored: masking is decided by field name at every depth, so there
    is no longer a list of "person" keys to get wrong.
    """
    return mask_payload(payload, policy)


def mask_for(user, payload, node=None):
    """Mask `payload` for this operator.

    One call at the edge of a view. The renderer makes the same call for
    every operator response, so a view that calls this is masked twice and
    that is harmless: every masker is idempotent.
    """
    return mask_payload(payload, policy_for(user, node))


# ---------------------------------------------------------------------------
# The way out: a reveal, marked on the request and written to the log
# ---------------------------------------------------------------------------

#: The request attribute the renderer honours. Set ONLY by reveal views.
REVEALED_ATTR = 'pii_revealed'


def mark_revealed(request, reason='reveal'):
    """Tell the renderer this response may carry unmasked personal data.

    The only legitimate callers are the reveal endpoints, which have
    already written the audit row by the time they call this. A list or
    detail view must never call it; `tests/test_pii_masking_everywhere`
    asserts the set of call sites.
    """
    setattr(request, REVEALED_ATTR, reason)
    # A DRF Request proxies attribute READS to the underlying HttpRequest
    # but a write lands on the wrapper, so set both: the renderer sees the
    # wrapper, middleware and logging see the HttpRequest.
    inner = getattr(request, '_request', None)
    if inner is not None:
        setattr(inner, REVEALED_ATTR, reason)


def is_revealed(request):
    return bool(getattr(request, REVEALED_ATTR, False))


def record_reveal(request, *, target_type, target_id, fields, reason='',
                  action=REVEAL_ACTION, node=None, extra=None):
    """One audit row per reveal: who, which record, WHICH fields. Never the
    values -- the audit log is read by more people than may see a phone
    number.

    The SOS incident reveal and the generic reveal both write through here
    so an access review reads one shape: `after.fields` lists what was
    handed over, `after.target_type` repeats the target for a query that
    only has the `after` column, `after.scope_node` says where the record
    sits. Returns the row, or None if the write failed.
    """
    from servers.admin_audit.services import record_admin_action

    after = {
        'masked': False,
        'fields': sorted(set(fields)),
        'target_type': target_type,
        'scope_node': getattr(node, 'code', None),
    }
    after.update(extra or {})
    row = record_admin_action(
        request,
        action=action,
        target_type=target_type,
        target_id=target_id,
        before={'masked': True},
        after=after,
        reason=reason,
    )
    if row is None:
        logger.error(
            'pii_reveal_unaudited action=%s target=%s#%s actor_id=%s fields=%s',
            action, target_type, target_id,
            getattr(getattr(request, 'user', None), 'id', None), sorted(set(fields)),
        )
    return row


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

def export_reveal_requested(request):
    """Did the caller ask for an unmasked export (`?reveal=1`)?"""
    params = getattr(request, 'query_params', None) or getattr(request, 'GET', {})
    return str(params.get('reveal', '')).strip().lower() in ('1', 'true', 'yes')


def exports_unmasked(request, *, target_type, row_count, reason='', node=None):
    """Decide whether a CSV/XLSX export may carry real personal data.

    An export is masked exactly like a list, unless the caller holds
    `pii.view` AND asked for it with `?reveal=1`, in which case ONE audit
    row (`pii_export_revealed`) records the export and its row count. A
    bulk hand-over is the most expensive thing this module can allow, so
    it is never the default and never silent. Returns True if the export
    may be unmasked; the caller then builds it from raw values and calls
    nothing else. There is no DRF admin endpoint producing an export today
    (the only CSV lives in the frozen session console); this is the rule
    the first one has to go through.
    """
    if not export_reveal_requested(request):
        return False
    from .bridge import granted

    allowed, _how = granted(getattr(request, 'user', None), 'pii.view', node)
    if not allowed:
        return False
    row = record_reveal(
        request, target_type=target_type, target_id='export',
        fields=['export'], reason=reason, action=EXPORT_REVEAL_ACTION,
        node=node, extra={'row_count': int(row_count)},
    )
    if row is None:
        # No log, no export: a bulk hand-over that left no trace is the
        # one outcome this function exists to make impossible.
        return False
    mark_revealed(request, 'export')
    return True


#: Patterns a response body must not contain once masked. Used by the
#: tests and by anybody auditing a captured body.
RAW_PII_PATTERNS = {
    'e164_phone': re.compile(r'\+\d{10,15}'),
    'long_digit_run': re.compile(r'\d{10,}'),
    'email': re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}'),
    'upi_handle': re.compile(r'(?<![A-Za-z0-9•])[A-Za-z0-9._-]{2,}@[a-z]{3,}\b'),
    'ifsc': re.compile(r'\b[A-Z]{4}0[A-Z0-9]{6}\b'),
}
