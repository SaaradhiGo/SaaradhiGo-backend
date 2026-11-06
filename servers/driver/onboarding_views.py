"""What this driver still has to supply before they can be approved.

  GET /api/v1/driver/driver/onboarding/   the driver's own checklist

WHY THIS ENDPOINT EXISTS
------------------------
Approval is gated on documents. `KYCApprovalSerializer` refuses
`approved=True` without a licence, a licence expiry in the future, an
assigned vehicle and that vehicle's registration; `compliance.py` decides
which documents a driver's COUNTRY requires and the operator console
reviews against that list.

A driver had no way to see any of it. The website offered no upload at all,
`/driver/driver/profile/` reported `approved: false` and nothing about why,
and the only path to a driving account was an app. So the honest answer to
"without documents, how are we approving a driver?" was: we are not, and we
could not -- nobody could reach approval through the website, and nobody was
told what was missing.

THE CHECKLIST IS COMPUTED, NOT WRITTEN DOWN TWICE
-------------------------------------------------
Every entry comes from `compliance.evaluate_driver` and
`compliance.evaluate_vehicle` -- the same functions the operator console
reviews against. A client that renders this cannot disagree with the
reviewer about what is required, and a country whose rules change changes
both screens at once.

`upload_kind` is included per document so the client can ask
`/uploads/presign/` for the right kind without keeping its own copy of the
mapping. A web page that hardcoded "licence means license_doc" would be a
third place to update.

WHAT IT DELIBERATELY DOES NOT DO
--------------------------------
It does not decide approval and it cannot be made to. It reports presence of
documents and the review state an operator set. `ready_for_review` means
"there is nothing obvious left to upload", not "this driver is approved" --
the gate is still `KYCApprovalSerializer`, behind an operator.
"""

import logging

from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes

from .permissions import IsDriver
from base.utils import success_response

from . import compliance
from .models import Vehicle, VehicleType

logger = logging.getLogger(__name__)

#: compliance key -> the `UPLOAD_KINDS` kind the client presigns against.
#: Both halves already exist; this is the one place they are joined, so a
#: client never has to know that "licence front" is stored as `license_doc`.
UPLOAD_KIND_FOR = {
    'license_front': 'license_doc',
    'license_back': 'license_doc_back',
    'rc': 'rc_doc',
    'registration': 'rc_doc',
    'insurance': 'insurance_doc',
    'permit': 'permit_doc',
    'fitness': 'fitness_doc',
    'puc': 'puc_doc',
    'inspection': 'inspection_doc',
}

#: Which endpoint a given document is submitted to once uploaded. The driver
#: documents go on the driver; the rest go on a vehicle.
TARGET_FOR = {
    'license_front': 'driver',
    'license_back': 'driver',
}


def _review_states(driver, vehicle, checks, scope):
    """`{key: (status, reason)}` from the operators' per-document reviews (2026-11-06).

    The console's own rule (admin_drivers._document_rows): a review counts only for the file it
    was about, so a re-upload is back to `in_review`. `approved` / `rejected` from that review,
    `in_review` for a file nobody has decided on, `missing` for no file. The reason is the
    reviewer's text on a rejection, the words the driver is meant to see.
    """
    from .models import DriverDocumentReview

    owner = vehicle if scope == 'vehicle' else driver
    columns = (compliance.VEHICLE_DOCUMENT_COLUMNS if scope == 'vehicle'
               else compliance.DRIVER_DOCUMENT_COLUMNS)
    latest = {}
    if owner is not None:
        for review in (DriverDocumentReview.objects
                       .filter(driver=driver, vehicle=vehicle if scope == 'vehicle' else None)
                       .order_by('reviewed_at', 'id')):
            latest[review.document_key] = review
    out = {}
    for key in checks:
        column = columns.get(key)
        field = getattr(owner, column, None) if (owner is not None and column) else None
        name = getattr(field, 'name', '') if field else ''
        review = latest.get(key)
        if not name:
            out[key] = ('missing', None)
        elif review is not None and review.file_name == name:
            out[key] = (review.state, (review.reason or None) if review.state == 'rejected' else None)
        else:
            out[key] = ('in_review', None)
    return out


def _entries(checks, scope, states=None):
    """Turn a `compliance.evaluate_*` result into a client-facing list."""
    out = []
    states = states or {}
    for key, check in checks.items():
        status_, reason = states.get(key, (None, None))
        out.append({
            # Per document (2026-11-06): approved, rejected, in_review or missing, and the
            # reviewer's reason on a rejection.
            'status': status_,
            'reason': reason,
            'key': key,
            'label': check['label'],
            'description': compliance.document_description(key),
            'required': check['required'],
            'present': check['present'],
            # False where the document has no column to store it in. Reported
            # rather than hidden: a requirement we cannot record is a
            # requirement nobody can satisfy, and the driver should be told
            # that instead of being left with a box that never ticks.
            'recordable': check['applicable'],
            'upload_kind': UPLOAD_KIND_FOR.get(key),
            'target': TARGET_FOR.get(key, scope),
        })
    out.sort(key=lambda e: (e['present'], e['label']))
    return out


@api_view(['GET'])
@permission_classes([IsDriver])
def driver_onboarding(request):
    """The driver's own onboarding checklist."""
    driver = request.user.driver

    vehicle = driver.active_vehicle
    if vehicle is None:
        # A driver with one vehicle and no active one chosen is the common
        # case right after adding it, and reviewing against "no vehicle"
        # would tell them to add the one they just added.
        vehicle = Vehicle.objects.filter(driver_id=driver).order_by('id').first()

    driver_checks = compliance.evaluate_driver(driver)
    driver_docs = _entries(driver_checks, 'driver',
                           _review_states(driver, None, driver_checks, 'driver'))
    if vehicle is not None:
        vehicle_checks = compliance.evaluate_vehicle(vehicle, driver)
        vehicle_docs = _entries(vehicle_checks, 'vehicle',
                                _review_states(driver, vehicle, vehicle_checks, 'vehicle'))
    else:
        vehicle_docs = []

    # The licence expiry is a FIELD, not a document, and the KYC gate checks
    # it separately -- a driver with a perfect photo of an expired licence is
    # refused. It is listed as its own step so that refusal is predictable.
    expiry_ok = bool(driver.license_expiry) and (
        driver.license_expiry > timezone.localdate()
    )

    outstanding = [d['key'] for d in driver_docs + vehicle_docs
                   if not (d['present'] and d['recordable'])]
    # The name is a FIELD too, and the KYC gate refuses a driver without a valid one
    # (AUDIT 27.71): listed first, so onboarding is never "complete" without it.
    from .serializers import has_valid_driver_name

    name_ok = has_valid_driver_name(request.user)
    if not name_ok:
        outstanding.insert(0, 'full_name')

    ready = (
        not outstanding
        and vehicle is not None
        and driver.active_vehicle is not None
        and expiry_ok
    )

    # THE CATALOGUE, so a page that lets a driver register a car does not
    # keep its own list of categories. `/pricing/vehicle-types/` is the
    # rider-facing one and needs a lat/lng -- it answers "what can I be
    # quoted HERE", which is a different question from "what kind of car do
    # you drive". A driver registering a vehicle has no pickup point.
    categories = [
        {'type': vt.type, 'description': getattr(vt, 'description', '') or ''}
        for vt in VehicleType.objects.all().order_by('id')
    ]

    return success_response(
        {
            'country': compliance.driver_country_code(driver),
            'vehicle_types': categories,
            'approved': driver.approved,
            'doc_status': driver.doc_status,
            'doc_rejection_reason': driver.doc_rejection_reason or '',
            'license_expiry': driver.license_expiry,
            'license_expiry_ok': expiry_ok,
            'full_name_ok': name_ok,
            'driver_documents': driver_docs,
            'vehicle': {
                'id': vehicle.id,
                'vehicle_number': vehicle.vehicle_number,
                'brand': vehicle.brand,
                'model': vehicle.model,
                'color': vehicle.color,
                'is_active': driver.active_vehicle_id == vehicle.id,
            } if vehicle is not None else None,
            'vehicle_documents': vehicle_docs,
            'outstanding': outstanding,
            # "Nothing obvious left to upload", NOT "approved". The gate is
            # still an operator behind KYCApprovalSerializer.
            'ready_for_review': ready,
        },
        status.HTTP_200_OK,
    )
