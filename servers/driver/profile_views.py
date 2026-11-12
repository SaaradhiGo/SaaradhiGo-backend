"""What a driver provides, from the web and the app (10 Nov). The market's requirements decide
what is asked (servers.driver.requirements); these endpoints save it.

  GET   requirements/            the market's config, the checklist, and the lists the forms need
  PATCH personal/                photo, legal name, date of birth, addresses, emergency contacts
  PATCH identity/                licence, Aadhaar (last 4 only), PAN, police verification
  GET   payout-method/           what is on file, masked
  PUT   payout-method/           India: bank account or UPI ID (a change pauses payouts 24 h)
  POST  payout-method/stripe/    USA: Stripe Connect Express onboarding link
  GET   vehicle-catalogue/       makes and models for the driver's country and a vehicle type
  GET   ifsc/<code>/             bank and branch for an IFSC

Files arrive as upload keys from /uploads/presign/ (the kinds in base.media.UPLOAD_KINDS), the
same path the existing documents use. Every new file is "In review" until an operator decides.
"""
import logging
import re
from datetime import date

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, parser_classes, permission_classes
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser

from base.countries import normalize_phone
from base.encrypted import mask
from base.media import resolve_file_input
from base.regions import is_state, postal_problem, states
from base.utils import error_response, success_response

from . import requirements as req
from .models import (
    DriverAddress, DriverBankAccount, DriverEmergencyContact, VehicleCatalogueEntry, VehicleType,
)
from .permissions import IsDriver
from servers.payments.payout_destination import payout_hold_until
from .review_target import target_days as review_target_days

logger = logging.getLogger(__name__)

UPLOAD_KIND = {
    'profile_photo': 'photo_pending', 'address_proof': 'address_proof_doc', 'aadhaar': 'aadhaar_doc',
    'pan': 'pan_doc', 'police_verification': 'police_verification_doc',
    'license_front': 'license_doc', 'license_back': 'license_doc_back',
    'rc': 'rc_doc', 'registration': 'rc_doc', 'insurance': 'insurance_doc', 'permit': 'permit_doc',
    'fitness': 'fitness_doc', 'puc': 'puc_doc', 'inspection': 'inspection_doc',
    'vehicle_photo_front': 'vehicle_pic', 'vehicle_photo_back': 'photo_back', 'vehicle_photo_left': 'photo_left',
    'vehicle_photo_right': 'photo_right', 'vehicle_photo_interior': 'photo_interior',
}
PAN_RE = re.compile(r'^[A-Z]{5}\d{4}[A-Z]$')
IFSC_RE = re.compile(r'^[A-Z]{4}0[A-Z0-9]{6}$')
UPI_RE = re.compile(r'^[a-zA-Z0-9.\-_]{2,256}@[a-zA-Z]{2,64}$')


def _bad(code, message, field):
    return error_response(code=code, message=message, field=field, issue=message,
                          status=status.HTTP_400_BAD_REQUEST)


def _country(driver):
    return req.for_driver(driver)['country']


def _address_dict(a):
    return {'line1': a.line1, 'line2': a.line2, 'city': a.city, 'state_code': a.state_code,
            'postal_code': a.postal_code} if a else None


def _url(field):
    """A public photo's URL, or None: a storage hiccup never breaks the page."""
    if not getattr(field, 'name', ''):
        return None
    try:
        return field.url
    except Exception:  # noqa: BLE001
        logger.warning('photo url unavailable')
        return None


def personal_dict(driver):
    user = driver.user_id
    addresses = {a.kind: a for a in DriverAddress.objects.filter(driver=driver)}
    return {
        'full_name': user.full_name or '', 'name_pending': driver.name_pending or None,
        'photo': _url(user.avatar),
        'photo_pending': _url(driver.photo_pending),
        'dob': user.dob.isoformat() if getattr(user, 'dob', None) else None,
        'address': _address_dict(addresses.get('current')),
        'permanent_address': _address_dict(addresses.get('permanent')),
        'emergency_contacts': [{'position': c.position, 'name': c.name, 'relationship': c.relationship,
                                'phone_number': c.phone_number}
                               for c in DriverEmergencyContact.objects.filter(driver=driver)],
        # The profile summary's city (10 Nov v2).
        'home_city': getattr(getattr(driver, 'home_city', None), 'name', None),
    }


def identity_dict(driver):
    return {
        'license_number': driver.license_number or '', 'license_state': driver.license_state or '',
        'license_expiry': driver.license_expiry.isoformat() if driver.license_expiry else None,
        'aadhaar_masked': f'XXXX XXXX {driver.aadhaar_last4}' if driver.aadhaar_last4 else None,
        'pan_masked': mask(driver.pan_number) if driver.pan_number else None,
        'police_verification_status': driver.police_verification_status,
        'background_check_status': driver.background_check_status,
    }


@api_view(['GET'])
@permission_classes([IsDriver])
def driver_requirements(request):
    driver = request.user.driver
    config = req.for_driver(driver)
    country = config['country']
    oldest, newest = req.year_range(config)
    return success_response({
        'config': config,
        'checklist': req.checklist(driver),
        # 12 Nov: "We usually review within N working days" reads the city's KYC review target.
        'review_target_working_days': __import__('servers.driver.review_target', fromlist=['x']).target_days(
            driver.home_city),
        'personal': personal_dict(driver),
        'identity': identity_dict(driver),
        'payout_method': payout_method_dict(driver, country),
        'states': states(country),
        'relationships': [{'code': c, 'label': l} for c, l in DriverEmergencyContact.RELATIONSHIPS],
        'vehicle_types': [vt.type for vt in VehicleType.objects.order_by('id')],
        'vehicle_years': list(range(newest, oldest - 1, -1)),
        'upload_kinds': UPLOAD_KIND,
    }, status.HTTP_200_OK)


def _address_from(data, country, field):
    line1 = str(data.get('line1') or '').strip()
    city = str(data.get('city') or '').strip()
    state_code = str(data.get('state_code') or '').strip().upper()
    postal = str(data.get('postal_code') or '').strip()
    if not line1 or not city:
        return None, _bad('ADDRESS_INCOMPLETE', 'Enter the address and city.', field)
    if not is_state(country, state_code):
        return None, _bad('ADDRESS_STATE', 'Choose a state from the list.', field)
    problem = postal_problem(country, state_code, postal)
    if problem:
        return None, _bad('ADDRESS_POSTAL', problem, field)
    return {'line1': line1[:256], 'line2': str(data.get('line2') or '').strip()[:256], 'city': city[:128],
            'state_code': state_code, 'postal_code': postal, 'country': country}, None


@api_view(['PATCH'])
@permission_classes([IsDriver])
@parser_classes([MultiPartParser, FormParser, JSONParser])
def driver_personal(request):
    """Any of: photo (upload key), full_name, dob (YYYY-MM-DD), address, permanent_address
    (or permanent_same: true), emergency_contacts [{position, name, relationship, phone_number}]."""
    from .serializers import is_valid_driver_name

    driver = request.user.driver
    user = driver.user_id
    config = req.for_driver(driver)
    country = config['country']
    d = request.data or {}
    driver_fields, user_fields = [], []

    provided, value, err = resolve_file_input(request, 'photo', kind='photo_pending')
    if err:
        return _bad('UPLOAD_FAILED', err, 'photo')
    if provided and value:
        driver.photo_pending = value
        driver_fields.append('photo_pending')

    if 'full_name' in d:
        name = ' '.join(str(d.get('full_name') or '').split())[:256]
        if not is_valid_driver_name(name):
            return _bad('NAME_INVALID', 'Enter your full name as on your licence.', 'full_name')
        if name != (user.full_name or ''):
            # A name riders and reviewers have seen changes only after review.
            if driver.approved and (user.full_name or '').strip():
                driver.name_pending = name
                driver_fields.append('name_pending')
            else:
                user.full_name = name
                user_fields.append('full_name')

    if 'dob' in d:
        try:
            dob = date.fromisoformat(str(d.get('dob')))
        except ValueError:
            return _bad('DOB_INVALID', 'Enter your date of birth.', 'dob')
        today = timezone.localdate()
        minimum = config.get('min_driver_age')
        if dob > today or req.age_on(dob, today) > 100:
            return _bad('DOB_INVALID', 'Enter your date of birth.', 'dob')
        if minimum and req.age_on(dob, today) < minimum:
            return _bad('DOB_TOO_YOUNG', f'You must be at least {minimum} to drive here.', 'dob')
        user.dob = dob
        user_fields.append('dob')

    addresses = {}
    for kind, field in (('current', 'address'), ('permanent', 'permanent_address')):
        if isinstance(d.get(field), dict):
            values, problem = _address_from(d[field], country, field)
            if problem:
                return problem
            addresses[kind] = values
    if d.get('permanent_same'):
        current = addresses.get('current') or _address_dict(
            DriverAddress.objects.filter(driver=driver, kind='current').first())
        if not current:
            return _bad('ADDRESS_INCOMPLETE', 'Enter your current address first.', 'permanent_address')
        addresses['permanent'] = {**current, 'country': country}

    contacts = None
    if isinstance(d.get('emergency_contacts'), list):
        contacts = []
        codes = {c for c, _ in DriverEmergencyContact.RELATIONSHIPS}
        for i, c in enumerate(d['emergency_contacts'][:2], start=1):
            name = ' '.join(str((c or {}).get('name') or '').split())[:128]
            rel = str((c or {}).get('relationship') or '')
            phone = normalize_phone(str((c or {}).get('phone_number') or ''), default_country=country)
            if not name:
                return _bad('CONTACT_NAME', f'Enter the name of contact {i}.', 'emergency_contacts')
            if rel not in codes:
                return _bad('CONTACT_RELATIONSHIP', f'Choose how contact {i} is related to you.',
                            'emergency_contacts')
            if not phone or not phone.startswith('+1' if country == 'US' else '+91'):
                return _bad('CONTACT_PHONE', f'Enter a valid mobile number for contact {i}.',
                            'emergency_contacts')
            if phone == user.phone_number:
                return _bad('CONTACT_PHONE', 'An emergency contact cannot be your own number.',
                            'emergency_contacts')
            contacts.append({'position': i, 'name': name, 'relationship': rel, 'phone_number': phone})

    with transaction.atomic():
        if driver_fields:
            driver.save(update_fields=driver_fields)
        if user_fields:
            user.save(update_fields=user_fields)
        for kind, values in addresses.items():
            DriverAddress.objects.update_or_create(driver=driver, kind=kind, defaults=values)
        if contacts is not None:
            DriverEmergencyContact.objects.filter(driver=driver).exclude(
                position__in=[c['position'] for c in contacts]).delete()
            for c in contacts:
                DriverEmergencyContact.objects.update_or_create(
                    driver=driver, position=c['position'],
                    defaults={k: v for k, v in c.items() if k != 'position'})
            if contacts and not user.emergency_contact:
                user.emergency_contact = contacts[0]['phone_number']
                user.save(update_fields=['emergency_contact'])
    return success_response({'personal': personal_dict(driver), 'checklist': req.checklist(driver)},
                            status.HTTP_200_OK)


@api_view(['PATCH'])
@permission_classes([IsDriver])
@parser_classes([MultiPartParser, FormParser, JSONParser])
def driver_identity(request):
    """Any of: license_number, license_state (US), license_expiry, aadhaar_last4 + aadhaar_doc,
    pan_number + pan_doc, police_verification_doc. A full Aadhaar number is refused: only the last
    4 digits are ever stored."""
    driver = request.user.driver
    country = _country(driver)
    d = request.data or {}
    fields = []
    if 'license_number' in d:
        number = re.sub(r'[\s\-]', '', str(d.get('license_number') or '').upper())
        if not re.fullmatch(r'[A-Z0-9]{5,20}', number):
            return _bad('LICENSE_NUMBER', 'Enter your licence number as printed.', 'license_number')
        driver.license_number = number
        fields.append('license_number')
    if 'license_state' in d:
        code = str(d.get('license_state') or '').upper()
        if not is_state(country, code):
            return _bad('LICENSE_STATE', 'Choose the state that issued your licence.', 'license_state')
        driver.license_state = code
        fields.append('license_state')
    if 'license_expiry' in d:
        try:
            driver.license_expiry = date.fromisoformat(str(d.get('license_expiry')))
        except ValueError:
            return _bad('LICENSE_EXPIRY', 'Enter the expiry date.', 'license_expiry')
        fields.append('license_expiry')
    if 'aadhaar_last4' in d:
        digits = re.sub(r'\D', '', str(d.get('aadhaar_last4') or ''))
        if len(digits) != 4:
            return _bad('AADHAAR_LAST4', 'Enter only the last 4 digits of your Aadhaar.', 'aadhaar_last4')
        driver.aadhaar_last4 = digits
        fields.append('aadhaar_last4')
    if 'pan_number' in d:
        pan = str(d.get('pan_number') or '').strip().upper()
        if not PAN_RE.match(pan):
            return _bad('PAN_INVALID', 'Enter your 10-character PAN, like ABCDE1234F.', 'pan_number')
        driver.pan_number = pan
        fields.append('pan_number')
    for name, kind in (('license_doc', 'license_doc'), ('license_doc_back', 'license_doc_back'),
                       ('aadhaar_doc', 'aadhaar_doc'), ('pan_doc', 'pan_doc'),
                       ('police_verification_doc', 'police_verification_doc'),
                       ('address_proof_doc', 'address_proof_doc')):
        provided, value, err = resolve_file_input(request, name, kind=kind)
        if err:
            return _bad('UPLOAD_FAILED', err, name)
        if provided and value:
            setattr(driver, name, value)
            fields.append(name)
            if name == 'police_verification_doc':
                driver.police_verification_status = 'pending'
                fields.append('police_verification_status')
    if fields:
        driver.save(update_fields=list(dict.fromkeys(fields)))
    return success_response({'identity': identity_dict(driver), 'checklist': req.checklist(driver)},
                            status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# Payout method
# ---------------------------------------------------------------------------

def payout_method_dict(driver, country):
    hold = payout_hold_until(driver)
    if country == 'US':
        return {'kind': 'stripe', 'stripe_status': driver.stripe_onboarding_status, 'held_until': None}
    bank = DriverBankAccount.objects.filter(driver=driver).first()
    return {
        'kind': 'bank' if bank else ('upi' if (driver.upi_id or '').strip() else None),
        'bank': ({'holder_name': bank.holder_name, 'account_masked': mask(bank.account_number),
                  'ifsc': bank.ifsc_code, 'bank_name': bank.bank_name, 'branch': bank.branch,
                  'verification_status': bank.verification_status} if bank else None),
        'upi_masked': mask(driver.upi_id, 5) if (driver.upi_id or '').strip() else None,
        'held_until': hold.isoformat() if hold else None,
    }


def lookup_ifsc(code):
    """`{bank, branch, city}` for an IFSC, cached for a day; None if unknown or unreachable."""
    from integrations.payouts.driver_onboarding import ifsc_lookup

    code = (code or '').strip().upper()
    if not IFSC_RE.match(code):
        return None
    key = f'ifsc:{code}'
    got = cache.get(key)
    if got is not None:
        return got or None
    out = ifsc_lookup(code)
    if out is None:
        return None
    cache.set(key, out, 86400)
    return out or None


def razorpayx_configured():
    return all(getattr(settings, n, '') or '' for n in ('RAZORPAYX_KEY_ID', 'RAZORPAYX_KEY_SECRET',
                                                       'RAZORPAYX_ACCOUNT_NUMBER'))


def request_bank_validation(bank, driver):
    """RazorpayX fund-account validation (Rs 1 and a name match). Without keys the account is
    marked "Verification pending (config)" (config-required.md)."""
    from integrations.payouts.driver_onboarding import validate_bank_account

    if not razorpayx_configured():
        return 'pending_config'
    state, name = validate_bank_account(bank.holder_name, bank.ifsc_code, bank.account_number)
    if state == 'verified':
        bank.verified_name = name
    return state


def _changed(driver, method_label, had_details):
    """A change to existing details pauses payouts for 24 hours and tells the driver."""
    if not had_details:
        return
    driver.payout_details_changed_at = timezone.now()
    driver.save(update_fields=['payout_details_changed_at'])
    try:
        from servers.notifications.dispatcher import notify
        from servers.notifications.seed_data import PAYOUT_DETAILS_CHANGED

        notify(PAYOUT_DETAILS_CHANGED, driver.user_id, {'method': method_label},
               idempotency_key=f'payout-details-changed:{driver.pk}:{int(timezone.now().timestamp())}')
    except Exception:  # noqa: BLE001
        logger.exception('payout details change notice failed driver=%s', driver.pk)


@api_view(['GET', 'PUT'])
@permission_classes([IsDriver])
def driver_payout_method(request):
    """PUT {method: "bank", holder_name, account_number, account_number_confirm, ifsc} or
    {method: "upi", upi_id}. India only; a US driver uses POST payout-method/stripe/."""
    driver = request.user.driver
    country = _country(driver)
    if request.method == 'GET':
        return success_response(payout_method_dict(driver, country), status.HTTP_200_OK)
    if country == 'US':
        return _bad('USE_STRIPE', 'In the USA, set up payouts with Stripe.', 'method')
    d = request.data or {}
    method = d.get('method')
    had = DriverBankAccount.objects.filter(driver=driver).exists() or bool((driver.upi_id or '').strip())
    if method == 'upi':
        upi = str(d.get('upi_id') or '').strip()
        if not UPI_RE.match(upi):
            return _bad('UPI_INVALID', 'Enter a UPI ID like name@okaxis.', 'upi_id')
        if upi == (driver.upi_id or ''):
            return success_response(payout_method_dict(driver, country), status.HTTP_200_OK)
        with transaction.atomic():
            driver.upi_id = upi
            driver.save(update_fields=['upi_id'])
            DriverBankAccount.objects.filter(driver=driver).delete()
        _changed(driver, 'UPI ID', had)
        return success_response(payout_method_dict(driver, country), status.HTTP_200_OK)
    if method != 'bank':
        return _bad('METHOD_INVALID', 'Choose a bank account or UPI ID.', 'method')
    holder = ' '.join(str(d.get('holder_name') or '').split())[:256]
    number = re.sub(r'\s', '', str(d.get('account_number') or ''))
    confirm = re.sub(r'\s', '', str(d.get('account_number_confirm') or ''))
    ifsc = str(d.get('ifsc') or '').strip().upper()
    if not holder:
        return _bad('HOLDER_NAME', 'Enter the account holder name.', 'holder_name')
    if not re.fullmatch(r'\d{9,18}', number):
        return _bad('ACCOUNT_NUMBER', 'Enter an account number of 9 to 18 digits.', 'account_number')
    if number != confirm:
        return _bad('ACCOUNT_MISMATCH', 'The account numbers do not match.', 'account_number_confirm')
    if not IFSC_RE.match(ifsc):
        return _bad('IFSC_INVALID', 'Enter an 11-character IFSC, like HDFC0001234.', 'ifsc')
    found = lookup_ifsc(ifsc)
    current = DriverBankAccount.objects.filter(driver=driver).first()
    if current and current.account_number == number and current.ifsc_code == ifsc and current.holder_name == holder:
        return success_response(payout_method_dict(driver, country), status.HTTP_200_OK)
    with transaction.atomic():
        bank = current or DriverBankAccount(driver=driver)
        bank.holder_name, bank.account_number, bank.ifsc_code = holder, number, ifsc
        bank.bank_name = (found or {}).get('bank') or bank.bank_name or 'Bank'
        bank.branch = (found or {}).get('branch') or ''
        bank.verification_status = 'pending'
        bank.save()
    bank.verification_status = request_bank_validation(bank, driver)
    bank.save(update_fields=['verification_status', 'verified_name', 'account_fingerprint'])
    _changed(driver, 'bank account', had)
    return success_response(payout_method_dict(driver, country), status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([IsDriver])
def driver_stripe_onboarding(request):
    """USA: a Stripe Connect Express onboarding link. SaaradhiGo never sees the bank details or SSN.
    With no Stripe key, QA's fake provider (PAYOUT_FAKE_ALLOWED) returns a link to the site's own
    return page that marks onboarding done."""
    from . import stripe_connect

    driver = request.user.driver
    if _country(driver) != 'US':
        return _bad('NOT_US', 'Stripe payouts are for drivers in the USA.', 'country')
    try:
        url = stripe_connect.onboarding_link(driver, return_url=str((request.data or {}).get('return_url') or ''))
    except stripe_connect.NotConfigured:
        return error_response(code='STRIPE_NOT_CONFIGURED', message='US payouts are not set up yet.',
                              field='general', issue='STRIPE_SECRET_KEY missing',
                              status=status.HTTP_503_SERVICE_UNAVAILABLE)
    return success_response({'url': url, 'stripe_status': driver.stripe_onboarding_status}, status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([IsDriver])
def driver_stripe_refresh(request):
    """After the driver comes back from Stripe: read the account's status again."""
    from . import stripe_connect

    driver = request.user.driver
    stripe_connect.refresh_status(driver, fake_done=bool((request.data or {}).get('fake_done')))
    return success_response({'stripe_status': driver.stripe_onboarding_status}, status.HTTP_200_OK)


@api_view(['GET'])
@permission_classes([IsDriver])
def driver_ifsc(request, code):
    found = lookup_ifsc(code)
    if not found:
        return error_response(code='IFSC_UNKNOWN', message='We could not find that IFSC.', field='ifsc',
                              issue='Not found', status=status.HTTP_404_NOT_FOUND)
    return success_response(found, status.HTTP_200_OK)


@api_view(['GET'])
@permission_classes([IsDriver])
def vehicle_catalogue(request):
    """`{makes: [{make, models: [{id, model}]}]}` for the driver's country and ?type=."""
    driver = request.user.driver
    vt = str(request.query_params.get('type') or '')
    rows = (VehicleCatalogueEntry.objects.filter(country=_country(driver), vehicle_type=vt, is_active=True)
            .order_by('make', 'model').values('id', 'make', 'model'))
    makes = {}
    for r in rows:
        makes.setdefault(r['make'], []).append({'id': r['id'], 'model': r['model']})
    return success_response({'type': vt, 'makes': [{'make': m, 'models': v} for m, v in makes.items()],
                             'colours': COLOURS}, status.HTTP_200_OK)


COLOURS = ['White', 'Silver', 'Grey', 'Black', 'Blue', 'Red', 'Brown', 'Beige', 'Green', 'Yellow',
           'Orange', 'Gold', 'Maroon', 'Other']
