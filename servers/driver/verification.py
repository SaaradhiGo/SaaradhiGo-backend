"""Automated checks of a driver's papers, behind one interface (12 Nov, console QA).

    DOC_VERIFICATION_PROVIDER   ''   -> not connected (production until a provider is chosen)
                                fake -> the QA fake (the default outside production)
                                <name> of a real adapter in _REAL, once one is signed

India plan: an approved verification provider (one API) fronts Sarathi/Parivahan for the driving
licence, VAHAN for the RC, DigiLocker for Aadhaar (with the driver's consent), OCR for the name on
the document and a face match between the selfie and the licence photo. Police verification stays a
document the reviewer checks, recorded on Driver.police_verification_status.

A provider implements check(name, driver, vehicle) returning Result. The console runs every check
with "Run checks" and shows the latest result per check; nothing here approves anyone.
"""
import logging
from dataclasses import dataclass, field

from django.conf import settings

logger = logging.getLogger(__name__)

LABELS = {'name_match': 'Name on document', 'licence': 'Licence number (Sarathi)', 'rc': 'RC (VAHAN)',
          'aadhaar': 'Aadhaar (DigiLocker)', 'face_match': 'Face match', 'police': 'Police verification'}
VERIFIED, MISMATCH, NOT_FOUND, PENDING, ERROR, NOT_CONNECTED = (
    'verified', 'mismatch', 'not_found', 'pending', 'error', 'not_connected')


@dataclass
class Result:
    status: str
    detail: dict = field(default_factory=dict)


def _police(driver):
    status = (driver.police_verification_status or '').lower()
    return {'verified': VERIFIED, 'approved': VERIFIED, 'rejected': MISMATCH}.get(status, PENDING)


class FakeProvider:
    """QA only. Answers from what is on file, so the review screen shows realistic results; it
    contacts nobody and every result says it is a test result."""
    name = 'fake'

    def check(self, check, driver, vehicle):
        user = driver.user_id
        has_licence = bool(getattr(driver.license_doc, 'name', ''))
        if check == 'name_match':
            ok = bool((user.full_name or '').strip()) and has_licence
            return Result(VERIFIED if ok else NOT_FOUND, {'test': True})
        if check == 'licence':
            if not (driver.license_number or '').strip():
                return Result(NOT_FOUND, {'test': True, 'reason': 'No licence number on file'})
            return Result(VERIFIED, {'test': True, 'valid_until': driver.license_expiry.isoformat()
                                     if driver.license_expiry else None})
        if check == 'rc':
            if vehicle is None or not vehicle.vehicle_number:
                return Result(NOT_FOUND, {'test': True, 'reason': 'No vehicle on file'})
            return Result(VERIFIED, {'test': True})
        if check == 'aadhaar':
            return Result(VERIFIED if driver.aadhaar_last4 else NOT_FOUND, {'test': True})
        if check == 'face_match':
            ok = has_licence and bool(getattr(getattr(user, 'avatar', None), 'name', '') or getattr(user, 'avatar', None))
            return Result(VERIFIED if ok else NOT_FOUND, {'test': True, 'score': 0.93 if ok else None})
        if check == 'police':
            return Result(_police(driver), {'test': True})
        return Result(ERROR, {'reason': 'unknown check'})


#: name -> "module:Class" of real adapters. Empty until a provider is signed (config-required.md).
_REAL = {}


def provider_name():
    default = '' if getattr(settings, 'IS_PRODUCTION', True) else 'fake'
    return (getattr(settings, 'DOC_VERIFICATION_PROVIDER', None) or default).strip().lower()


def get_provider():
    name = provider_name()
    if name == 'fake' and not getattr(settings, 'IS_PRODUCTION', True):
        return FakeProvider()
    if name in _REAL:
        import importlib

        module, cls = _REAL[name].split(':')
        return getattr(importlib.import_module(module), cls)()
    return None


def run_all(driver, by=None):
    """Run every check now and store the results. Returns them."""
    from .models import DocumentVerification

    provider = get_provider()
    vehicle = driver.active_vehicle
    out = []
    for check in DocumentVerification.CHECKS:
        if provider is None:
            result = Result(_police(driver) if check == 'police' else NOT_CONNECTED, {})
        else:
            try:
                result = provider.check(check, driver, vehicle)
            except Exception:  # noqa: BLE001 - one failing check never hides the others
                logger.exception('verification %s failed for driver %s', check, driver.pk)
                result = Result(ERROR, {})
        out.append(DocumentVerification.objects.create(
            driver=driver, vehicle=vehicle if check == 'rc' else None, kind=check, status=result.status,
            provider=getattr(provider, 'name', '') or 'none', detail=result.detail, checked_by=by))
    return out


def latest(driver):
    """The console's view: one row per check (newest result), or not connected / not run yet."""
    from .models import DocumentVerification

    rows = {}
    for v in DocumentVerification.objects.filter(driver=driver).order_by('-checked_at', '-id')[:60]:
        rows.setdefault(v.kind, v)
    name = provider_name()
    connected = get_provider() is not None
    out = []
    for check in DocumentVerification.CHECKS:
        v = rows.get(check)
        detail = (v.detail or {}) if v else {}
        out.append({'check': check, 'label': LABELS[check],
                    'status': v.status if v else (NOT_CONNECTED if not connected else 'not_run'),
                    'test_result': bool(detail.get('test')),
                    'detail': {k: x for k, x in detail.items() if k in ('reason', 'score', 'valid_until')},
                    'checked_at': v.checked_at.isoformat() if v else None})
    return {'provider': name or None, 'connected': connected, 'test_provider': name == 'fake', 'checks': out}
