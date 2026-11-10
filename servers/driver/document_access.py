"""How a driver's KYC documents are opened: on demand, scoped, short-lived, audited.

AUDIT 27.78, DRIVER DOCUMENTS REVIEW
------------------------------------
Licence, RC, insurance, permit, fitness, PUC and inspection files are
identity documents. Before this module:

  * every driver detail and the KYC QUEUE (a list, twenty drivers a page)
    embedded a presigned GET for every file, signed for AWS_QUERYSTRING_EXPIRE
    (15 minutes), whether or not anybody opened one;
  * `GET /driver/admin/<id>/` and `/vehicles/` serialized the same links
    through DRF FileFields, gated only by `driver.read` -- the renderer masks
    documents by "pii.view held ANYWHERE", so an operator with pii.view in one
    city saw another city's documents;
  * the driver's own profile and vehicle list carried 15-minute links too;
  * no view of any document was recorded anywhere.

THE RULES NOW
-------------
  * Responses carry no bucket URL. Operator responses say whether a document
    is on file and whether this operator may open it; the driver's own
    responses carry a `view link` instead of a bucket URL (below).
  * A bucket URL is minted only when somebody opens a document, and lives
    `url_ttl()` seconds: at most 300, whatever the environment says.
  * An operator opens a document through
    `GET /driver/admin/<id>/documents/<key>/view/`: `driver.read` in scope
    (else 404, like every scoped read) and `pii.view` AT THE DRIVER'S CITY
    (else 403). A driver opens only their own, through the view link.
  * Every open writes an AdminAuditLog row -- who, which driver, which
    document, how -- BEFORE the URL is handed over, and never the URL. If the
    row cannot be written the document is not opened (503).

WHY THE DRIVER GETS A VIEW LINK, NOT AN API CALL
------------------------------------------------
The shipped driver app shows its documents by loading the string in
`license_doc` / `active_vehicle_details.rc_doc` as an image URL. A view link
keeps that contract with no app release: it is a backend URL carrying a
signed ticket (this driver, this document, five minutes), and opening it
writes the audit row and redirects to a freshly signed bucket URL. Nothing in
the profile response can be used to fetch a file after five minutes, and
nothing in it names the bucket.
"""

import logging
from types import SimpleNamespace

from django.conf import settings
from django.core import signing

logger = logging.getLogger(__name__)

#: The hard ceiling on a document URL's life, in seconds.
MAX_URL_TTL = 300

VIEWED_ACTION = 'driver_document_viewed'

_TICKET_SALT = 'saaradhigo.driver.document_view.v1'

#: Columns that hold a private document, per owner.
DRIVER_COLUMNS = ('license_doc', 'license_doc_back', 'health_insurance_doc',
                  'term_insurance_doc', 'induction_training_certificate')
VEHICLE_COLUMNS = ('rc_doc', 'permit_doc', 'insurance_doc', 'fitness_doc',
                   'puc_doc', 'inspection_doc')


def url_ttl():
    """Seconds a signed document URL lives: `DOCUMENT_URL_TTL_SECONDS`, never over 300."""
    try:
        configured = int(getattr(settings, 'DOCUMENT_URL_TTL_SECONDS', MAX_URL_TTL))
    except (TypeError, ValueError):
        configured = MAX_URL_TTL
    return max(30, min(configured, MAX_URL_TTL))


def signed_url(field):
    """A presigned GET for one stored document, valid `url_ttl()` seconds, or None.

    Only a storage that can sign with an explicit expiry is used: a storage
    without one (a public or custom-domain store) would hand out a permanent
    URL, and None is the safer answer. Never logs the URL.
    """
    name = getattr(field, 'name', '') if field else ''
    if not name:
        return None
    storage = getattr(field, 'storage', None)
    if storage is None or getattr(storage, 'querystring_auth', True) is False \
            or getattr(storage, 'custom_domain', None):
        logger.error('document_url_refused reason=unsigned_storage')
        return None
    try:
        return storage.url(name, expire=url_ttl())
    except TypeError:
        logger.error('document_url_refused reason=storage_cannot_expire')
        return None
    except Exception:  # noqa: BLE001 - a missing link beats a failed page
        logger.exception('document_url_failed')
        return None


def column_for(key):
    """(owner, column) for a compliance key (`rc`, `license_front`) or a column name."""
    from . import compliance

    if key in compliance.DRIVER_DOCUMENT_COLUMNS:
        return 'driver', compliance.DRIVER_DOCUMENT_COLUMNS[key]
    if key in compliance.VEHICLE_DOCUMENT_COLUMNS:
        return 'vehicle', compliance.VEHICLE_DOCUMENT_COLUMNS[key]
    if key in DRIVER_COLUMNS:
        return 'driver', key
    if key in VEHICLE_COLUMNS:
        return 'vehicle', key
    return None, None


def stored_document(driver, key, vehicle_id=None):
    """(field, vehicle) for one document of `driver`, or (None, None)."""
    owner, column = column_for(key)
    if owner == 'driver':
        field = getattr(driver, column, None)
        return (field, None) if field else (None, None)
    if owner == 'vehicle':
        if vehicle_id in (None, ''):
            return None, None
        try:
            vehicle = driver.vehicle_set.filter(pk=int(vehicle_id)).first()
        except (TypeError, ValueError):
            return None, None
        field = getattr(vehicle, column, None) if vehicle else None
        return (field, vehicle) if field else (None, None)
    return None, None


def record_view(request, *, actor, driver, key, vehicle, via):
    """The audit row for one document open. Returns the row, or None if it failed."""
    from servers.admin_audit.services import record_admin_action

    _owner, column = column_for(key)
    who = request if getattr(request, 'user', None) is actor else SimpleNamespace(
        user=actor, META=getattr(request, 'META', {}))
    return record_admin_action(
        who, action=VIEWED_ACTION, target_type='driver', target_id=driver.pk,
        after={'document_key': key, 'column': column,
               'vehicle_id': getattr(vehicle, 'pk', None), 'via': via},
        reason=f'Opened {column}',
    )


# ---------------------------------------------------------------------------
# The driver's own view link
# ---------------------------------------------------------------------------

VIEW_PATH = '/api/v1/driver/driver/documents/view/{ticket}/'


def view_link(request, driver, column, vehicle=None):
    """A five-minute link through which `driver`'s own account opens one document.

    Absolute when a request is available, so an image widget can load it.
    """
    ticket = signing.dumps(
        {'u': driver.user_id_id, 'd': driver.pk, 'c': column,
         'v': getattr(vehicle, 'pk', None)},
        salt=_TICKET_SALT, compress=True,
    )
    path = VIEW_PATH.format(ticket=ticket)
    if request is not None and hasattr(request, 'build_absolute_uri'):
        return request.build_absolute_uri(path)
    return path


def read_ticket(ticket):
    """The ticket's payload, or None when it is forged or older than `url_ttl()`."""
    try:
        payload = signing.loads(ticket, salt=_TICKET_SALT, max_age=url_ttl())
    except signing.BadSignature:
        return None
    return payload if isinstance(payload, dict) else None
