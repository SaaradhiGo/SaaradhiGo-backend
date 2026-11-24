"""What an approval request changes, in one plain sentence (owner 24 Nov).

"Hyderabad daily driver fee: Auto ₹20, cars ₹49 a day" rather than the action's generic title.
Unknown payloads fall back to the title, so a new action never shows a blank line.
"""

from servers.admin_access.models import GeoNode

COUNTRY_NAMES = {'IN': 'India', 'US': 'United States'}
CLASS_WORDS = {'auto': 'Auto', 'bike': 'Bike', 'sedan': 'Cars', 'suv': 'SUVs', 'hatchback': 'Hatchbacks', '*': 'Every class'}


def place_name(code):
    """'Hyderabad' for 'IN-TG-HYD', 'India' for 'IN'; the code when the place is unknown."""
    if not code:
        return 'Every place'
    if code in COUNTRY_NAMES:
        return COUNTRY_NAMES[code]
    node = GeoNode.objects.filter(code=code).only('name').first()
    return node.name if node else code


def _fee_plan(payload):
    from servers.pricing import fee_plans
    from servers.pricing.fee_plan_models import DAILY_PASS, PERCENT_COMMISSION, DriverFeePlan

    plan = DriverFeePlan.objects.filter(pk=payload.get('plan_id')).first()
    if plan is None:
        return None
    place = place_name(plan.scope_code or plan.country)
    kind = {DAILY_PASS: 'daily driver fee', PERCENT_COMMISSION: 'driver fee (commission)'}.get(
        plan.plan_type, 'per-trip driver fee')
    parts = []
    for vt in sorted((plan.amounts or {}).keys(), key=lambda k: (k == '*', k)):
        words = fee_plans.plan_words(plan, vt if vt != '*' else '', plan.country) or ''
        parts.append(f"{CLASS_WORDS.get(vt, vt.title())} {words.replace(', only on a day with a trip', '')}")
    return f"{place} {kind}: {', '.join(parts) or 'no amounts yet'}"


def _fare_card(payload):
    values = payload.get('values') or {}
    vt = CLASS_WORDS.get(payload.get('vehicle_type_name') or '', payload.get('vehicle_type_name') or 'a class')
    from servers.pricing.models import ServiceZone

    z = ServiceZone.objects.filter(pk=payload.get('zone')).only('name').first()
    zone = z.name if z else (payload.get('zone_code') or 'a zone')
    if payload.get('kind') == 'rate_card_retire':
        return f'Retire the {vt} fare card in {zone}'
    bits = [f'{k.replace("_", " ")} {str(v).removesuffix(".00")}' for k, v in list(values.items())[:4]]
    return f"{vt} fare card in {zone}: {', '.join(bits)}" if bits else f'New {vt} fare card in {zone}'


def _commission_rule(payload):
    place = place_name(payload.get('scope') or payload.get('country'))
    return f"{place} driver fee (commission rule): {payload.get('percent')}% of each fare"


def describe(row, title):
    """One sentence saying exactly what the request changes; `title` when the payload is unknown."""
    payload = row.payload or {}
    kind = payload.get('kind')
    try:
        if kind == 'driver_fee_plan':
            return _fee_plan(payload) or title
        if kind in ('rate_card_create', 'rate_card_retire'):
            return _fare_card(payload)
        if kind == 'commission_rule':
            return _commission_rule(payload)
    except Exception:  # noqa: BLE001 - a summary must never break the approvals list
        return title
    return title


SYSTEM_WORDS = {
    'system:weekly_payout': 'Weekly payout run (automatic)',
    'system:approval_expiry': 'Approval expiry (automatic)',
    'system:alert_thresholds': 'Alert check (automatic)',
    'system:fraud_money': 'Fraud money hold (automatic)',
    'system:owner_ruling': 'Owner ruling',
    'system:migration': 'Data migration (automatic)',
    'system:genesis': 'Audit log start (automatic)',
}


def actor_words(label):
    """'Weekly payout run (automatic)' for 'system:weekly_payout'; a person's label unchanged."""
    if not label or not str(label).startswith('system:'):
        return label
    return SYSTEM_WORDS.get(label) or f"{label.split(':', 1)[1].replace('_', ' ').capitalize()} (automatic)"
