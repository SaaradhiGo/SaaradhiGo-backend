"""The legal entity that operates SaaradhiGo in each market (13 Nov, Srini's rulings of 12 Nov).

    India  DeepTrics Software Solutions Pvt Ltd
    USA    DeepTrics LLC

Everything else (address, company number, support and grievance contacts, GST registration) is
configuration: `LEGAL_ENTITIES_JSON`, a JSON object keyed by country ("IN") or state ("IN-TG"),
merged over the defaults below. A state key overrides its country field by field. A value that is
not configured is left OUT of every document and page; nothing is invented
(docs/config-required.md lists what is missing).

GST. `gst_registered` is False until Srini says otherwise. While it is False the rider document is
a "Receipt" with no GST lines and no GSTIN; the amount the rider pays does not change. Switching it
on brings the tax-invoice layout back. It applies to markets whose tax is included in the fare
(India). An additive market (the USA) charges its tax as a separate amount, which the receipt
always shows.
"""

import json
import logging

from django.conf import settings

logger = logging.getLogger(__name__)

#: Every field a market's entity can carry, in display order.
FIELDS = (
    'legal_name', 'registered_address',
    # India: CIN. USA: state of formation and the state's file number.
    'cin', 'state_of_formation', 'file_number',
    'support_email', 'support_phone',
    # India (IT Rules 2021 / DPDP Act): the grievance officer.
    'grievance_officer_name', 'grievance_officer_email', 'grievance_officer_phone',
    'gst_registered', 'gstin',
)

#: Srini's rulings (12 Nov). Only the names are given; the rest stays unset until configured.
DEFAULTS = {
    'IN': {'legal_name': 'DeepTrics Software Solutions Pvt Ltd', 'gst_registered': False},
    'US': {'legal_name': 'DeepTrics LLC', 'gst_registered': False},
}
FALLBACK_COUNTRY = 'IN'


def _configured():
    raw = getattr(settings, 'LEGAL_ENTITIES_JSON', '') or ''
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        logger.error('LEGAL_ENTITIES_JSON is not valid JSON; using the defaults only')
        return {}
    return {str(k).upper(): v for k, v in data.items() if isinstance(v, dict)}


def _clean(values):
    out = {}
    for field in FIELDS:
        if field not in values:
            continue
        value = values[field]
        if field == 'gst_registered':
            out[field] = value is True or str(value).strip().lower() in ('true', '1', 'yes')
        elif value not in (None, ''):
            out[field] = str(value).strip()
    return out


def entity(country=None, state=None):
    """The entity for a market: country defaults, then configured country, then configured state.

    Unknown countries fall back to India (the brief's last resort). Keys whose value is not set
    are absent, never empty strings."""
    code = str(country or '').upper()[:2] or FALLBACK_COUNTRY
    if code not in DEFAULTS:
        code = FALLBACK_COUNTRY
    configured = _configured()
    merged = dict(DEFAULTS[code])
    if code == 'IN':
        # The settings that existed before 13 Nov still count for India.
        for field, name in (('registered_address', 'PLATFORM_LEGAL_ADDRESS'), ('gstin', 'PLATFORM_GSTIN')):
            value = (getattr(settings, name, '') or '').strip()
            if value:
                merged[field] = value
    merged.update(_clean(configured.get(code, {})))
    state_code = str(state or '').upper()
    if state_code and not state_code.startswith(code + '-'):
        state_code = f'{code}-{state_code}'
    if state_code in configured:
        merged.update(_clean(configured[state_code]))
    merged = _clean(merged)
    merged.setdefault('gst_registered', False)
    if not merged['gst_registered']:
        merged.pop('gstin', None)   # an unregistered entity never prints a GSTIN
    merged['country'] = code
    return merged


def all_markets():
    return {code: entity(code) for code in DEFAULTS}


def operated_by(country=None, state=None):
    """'Operated by DeepTrics Software Solutions Pvt Ltd.'"""
    return f"Operated by {entity(country, state)['legal_name']}."


def gst_registered(country=None, state=None):
    return bool(entity(country, state).get('gst_registered'))


def entity_for_zone(zone):
    country = getattr(zone, 'country', None) if zone is not None else None
    state = getattr(zone, 'state_code', None) if zone is not None else None
    return entity(country, state)


def entity_for_trip(trip):
    return entity_for_zone(getattr(trip, 'zone', None))


def tax_shown(zone):
    """False when the zone's tax is included in the fare and its entity is not GST-registered:
    the rider document then has no tax lines. Additive tax is a real separate charge: always shown."""
    mode = (getattr(zone, 'tax_mode', '') or 'inclusive') if zone is not None else 'inclusive'
    if mode == 'additive':
        return True
    return gst_registered(getattr(zone, 'country', None), getattr(zone, 'state_code', None))


def missing(country):
    """Field names not configured for a market (config-required.md and the startup report)."""
    e = entity(country)
    wanted = ['registered_address', 'support_email', 'support_phone']
    if country == 'IN':
        wanted += ['cin', 'grievance_officer_name', 'grievance_officer_email',
                   'grievance_officer_phone']
    else:
        wanted += ['state_of_formation', 'file_number']
    if e.get('gst_registered') and country == 'IN':
        wanted.append('gstin')
    return [f for f in wanted if not e.get(f)]
