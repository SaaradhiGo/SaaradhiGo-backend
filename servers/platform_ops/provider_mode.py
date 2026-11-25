"""Provider modes, switched from the console (owner ruling, 25 Nov).

Per provider type and country, an approved mode: `live`, `test` or `off`. Keys stay in the
environment and are never on screen; the switch only says which way to use them.

    live  the real provider, when its keys are present AND the environment allows live sending
          (so QA can never go live by a click: its environment keeps live sending off)
    test  the test sender: messages are recorded and nobody receives them; calls use the fake bridge
    off   nothing is sent; calls are refused

No row = the environment decides, exactly as before. A change is a `platform.provider_mode`
request, always with a second person who holds platform.settings; the approval shows before/after.
"""

from django.db import transaction

from servers.admin_audit.services import record_admin_action
from servers.approvals import registry
from servers.approvals.service import InvalidRequest, require_approval_or_execute

PROVIDER_MODE_CHANGE = 'platform.provider_mode'
MODES = ('live', 'test', 'off')
#: The types the switch governs: notification channels and calling.
SWITCHABLE = ('sms', 'email', 'push', 'whatsapp', 'calling')
WORDS = {'live': 'Live', 'test': 'Test (nobody receives it)', 'off': 'Off (nothing is sent)', None: 'Set by the environment'}


def mode_for(ptype, country):
    """The approved mode, or None when the environment decides."""
    from .models import ProviderMode

    try:
        row = ProviderMode.objects.filter(provider_type=ptype, country=str(country or '').upper(),
                                          is_active=True).only('mode').first()
    except Exception:  # noqa: BLE001 - a missing table (old migration state) never breaks a send
        return None
    return row.mode if row else None


def propose(actor, *, ptype, country, mode, reason, http_request=None):
    from servers.approvals.scope import country_node, may_act
    from servers.approvals.service import NotPermitted

    country = str(country or '').upper()
    if ptype not in SWITCHABLE:
        raise InvalidRequest('This provider is set by its keys in the environment.', code='NOT_SWITCHABLE',
                             field='type')
    if mode not in MODES:
        raise InvalidRequest('Mode is live, test or off.', code='INVALID_MODE', field='mode')
    if len((reason or '').strip()) < 5:
        raise InvalidRequest('Say why, in a few words.', code='REASON_REQUIRED', field='reason')
    node = country_node(country)
    if node is None or not may_act(actor, 'platform.settings', node):
        raise NotPermitted(f'You do not hold platform.settings over {country}.', code='SCOPE_NOT_HELD',
                           field='country')
    current = mode_for(ptype, country)
    if current == mode:
        raise InvalidRequest('That is already the mode.', code='NO_CHANGE', field='mode')
    display = {'changes': [{'label': f'{ptype.upper() if ptype == "sms" else ptype.capitalize()} in {country}',
                            'from': WORDS[current], 'to': WORDS[mode]}],
               'effective': 'From approval. Keys stay in the environment; live also needs them there.'}
    return require_approval_or_execute(
        PROVIDER_MODE_CHANGE, actor=actor, country=country, scope_node=node,
        target_type='provider_mode', target_id=f'{ptype}:{country}',
        payload={'type': ptype, 'country': country, 'mode': mode, 'before': current, 'display': display},
        idempotency_key=f'provider_mode:{ptype}:{country}:{mode}', reason=reason, http_request=http_request)


@registry.register(PROVIDER_MODE_CHANGE)
def execute(request_row):
    from .models import ProviderMode

    p = request_row.payload or {}
    with transaction.atomic():
        current = (ProviderMode.objects.select_for_update()
                   .filter(provider_type=p['type'], country=p['country'], is_active=True).first())
        if current is not None:
            current.is_active = False
            current.save(update_fields=['is_active'])
        row = ProviderMode.objects.create(
            provider_type=p['type'], country=p['country'], mode=p['mode'], is_active=True,
            version=(current.version if current else 0) + 1, changed_via_request_id=request_row.id,
            reason=request_row.request_reason or '', created_by_label=request_row.maker_label or '')
        record_admin_action(None, action='provider_mode_changed', target_type='provider_mode',
                            target_id=f"{p['type']}:{p['country']}", before={'mode': current.mode if current else None},
                            after={'mode': row.mode, 'version': row.version},
                            reason=request_row.decision_reason or request_row.request_reason)
    return {'type': row.provider_type, 'country': row.country, 'mode': row.mode}
