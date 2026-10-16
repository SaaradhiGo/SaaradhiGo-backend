"""Which documents a driver must supply, in the country they drive in.

THE BLOCKER THIS REMOVES
------------------------
The operator console's KYC checklist hardcoded five Indian statutory documents and
required `all()` of them:

    rc        Registration Certificate
    insurance
    permit    commercial passenger permit
    fitness   Fitness Certificate
    puc       Pollution Under Control

Three of those have no US equivalent. A US driver cannot obtain a PUC certificate or
a commercial passenger permit, so `all(...)` could never be true, so
`all_vehicle_compliant` was permanently False, so **a US driver could never be
approved**. Not a bad error message — no possible path to approval.

So the required set comes from the country, and compliance means "has everything
their country requires", not "has everything India requires".

HOW A DRIVER'S COUNTRY IS DETERMINED
------------------------------------
From their phone number's country code.

That is deliberate and it is the only signal available at onboarding: a driver being
reviewed for approval has no trips yet, so there is no zone to infer from. The phone
number is E.164 and was normalised at signup against the supported-country registry,
which means it is a reliable discriminator rather than a guess.

A number from a country we do not serve falls back to the deployment default. That is
logged, because it means somebody is being reviewed against the wrong checklist.

WHERE THE DOCUMENTS ARE STORED
------------------------------
`registration` maps onto the existing `rc_doc` column -- India's Registration
Certificate and the US vehicle registration are the same document under two names --
and `inspection` has its own `inspection_doc` column, added for the US safety
inspection.

`unrecordable_documents()` below still exists and still counts an unrecordable
document as NOT compliant. It is empty today, and it is the mechanism that keeps the
next country's new document type from being silently treated as satisfied instead of
blocking approval visibly.
"""

import logging

from base.countries import DEFAULT_COUNTRY_CODE, country_of_phone, get_country

logger = logging.getLogger(__name__)

# How a country's abstract document key maps onto the columns that exist on
# `Vehicle` today. A key absent from here has no column yet.
#
# `registration` and `rc` are the same document under two names -- India's
# Registration Certificate is the US vehicle registration -- so they share a
# column rather than duplicating it.
VEHICLE_DOCUMENT_COLUMNS = {
    'rc': 'rc_doc',
    'registration': 'rc_doc',
    'insurance': 'insurance_doc',
    'permit': 'permit_doc',
    'fitness': 'fitness_doc',
    'puc': 'puc_doc',
    'inspection': 'inspection_doc',
}

DRIVER_DOCUMENT_COLUMNS = {
    'license_front': 'license_doc',
    'license_back': 'license_doc_back',
}

# Human labels for the console, so the checklist does not show raw keys.
DOCUMENT_LABELS = {
    'rc': 'Registration Certificate',
    'registration': 'Vehicle registration',
    'insurance': 'Insurance',
    'permit': 'Commercial permit',
    'fitness': 'Fitness certificate',
    'puc': 'Pollution Under Control',
    'inspection': 'Safety inspection',
    'license_front': 'Licence (front)',
    'license_back': 'Licence (back)',
}


# One line of operator-facing explanation per document, for the console
# checklist. Separate from DOCUMENT_LABELS because the label is a name and this
# is the "what is this and why do we want it" an operator reads.
DOCUMENT_DESCRIPTIONS = {
    'rc': 'Vehicle ownership and registration document issued by the RTO.',
    'registration': 'State vehicle registration showing current ownership.',
    'insurance': 'Valid commercial motor insurance for the vehicle.',
    'permit': 'Commercial passenger permit for the operating region.',
    'fitness': 'Fitness certificate confirming the vehicle is roadworthy.',
    'puc': 'Valid Pollution Under Control certificate.',
    'inspection': 'Periodic safety inspection certificate.',
    'license_front': 'Government-issued driving licence, front.',
    'license_back': 'Government-issued driving licence, reverse.',
}


def document_label(key):
    return DOCUMENT_LABELS.get(key, key)


def document_description(key):
    return DOCUMENT_DESCRIPTIONS.get(key, '')


def driver_country_code(driver):
    """The country whose rules this driver is reviewed against.

    Resolved from their E.164 phone number, which is the only signal available
    before they have taken a trip.
    """
    user = getattr(driver, 'user_id', None)
    phone = getattr(user, 'phone_number', None)
    country = country_of_phone(phone)
    if country is not None:
        return country.code
    if phone:
        logger.warning(
            'driver %s has a phone number in no supported country; reviewing '
            'against the %s checklist. They may be being asked for the wrong '
            'documents.',
            getattr(driver, 'pk', None), DEFAULT_COUNTRY_CODE,
        )
    return DEFAULT_COUNTRY_CODE


def required_vehicle_documents(driver):
    """Document keys this driver's vehicle must have, for their country."""
    return tuple(get_country(driver_country_code(driver)).vehicle_documents)


def required_driver_documents(driver):
    """Document keys this driver personally must have, for their country."""
    return tuple(get_country(driver_country_code(driver)).driver_documents)


def _has(obj, column):
    """Is a FileField actually populated?

    Mirrors the console's own `has_document`: a FileField is falsy when empty but
    truthy as an object even with a blank name, so `.name` is what matters.
    """
    if not column:
        return False
    field = getattr(obj, column, None)
    return bool(field and getattr(field, 'name', ''))


def evaluate_vehicle(vehicle, driver):
    """Per-document status for one vehicle, against its driver's country.

    Returns a dict of `key -> {'label', 'required', 'present', 'applicable'}`.

    `applicable=False` means the document has no column on this model yet, so it
    is reported as outstanding-but-unrecordable rather than silently passing.
    Pretending an unrecordable document is satisfied is how an unverified driver
    gets approved.
    """
    out = {}
    for key in required_vehicle_documents(driver):
        column = VEHICLE_DOCUMENT_COLUMNS.get(key)
        out[key] = {
            'label': DOCUMENT_LABELS.get(key, key),
            'required': True,
            'applicable': column is not None,
            'present': _has(vehicle, column) if column else False,
        }
    return out


def evaluate_driver(driver):
    """Per-document status for the driver themselves."""
    out = {}
    for key in required_driver_documents(driver):
        column = DRIVER_DOCUMENT_COLUMNS.get(key)
        out[key] = {
            'label': DOCUMENT_LABELS.get(key, key),
            'required': True,
            'applicable': column is not None,
            'present': _has(driver, column) if column else False,
        }
    return out


def vehicle_is_compliant(vehicle, driver):
    """Does this vehicle have everything its driver's country requires?

    A required document with no column to store it in counts as NOT compliant.
    The alternative -- treating unrecordable as satisfied -- would approve a US
    driver whose safety inspection nobody ever saw.
    """
    checks = evaluate_vehicle(vehicle, driver)
    return bool(checks) and all(
        c['present'] and c['applicable'] for c in checks.values()
    )


def driver_is_compliant(driver):
    checks = evaluate_driver(driver)
    return bool(checks) and all(
        c['present'] and c['applicable'] for c in checks.values()
    )


def missing_documents(vehicle, driver):
    """Keys that are required and not satisfied. For an operator-facing message."""
    missing = [k for k, c in evaluate_vehicle(vehicle, driver).items()
               if not (c['present'] and c['applicable'])]
    missing += [k for k, c in evaluate_driver(driver).items()
                if not (c['present'] and c['applicable'])]
    return missing


def unrecordable_documents(driver):
    """Required documents this country needs that have nowhere to be stored.

    Surfaced in the console so the gap is visible to an operator rather than
    being an unexplained permanent block. Today this is the US safety
    inspection.
    """
    return tuple(
        key for key in required_vehicle_documents(driver)
        if VEHICLE_DOCUMENT_COLUMNS.get(key) is None
    )
