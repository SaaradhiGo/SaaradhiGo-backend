"""Templates: drafting, validating and activating through maker-checker (`notification.template_change`).

NOTHING GOES LIVE BY ITSELF
---------------------------
A template is edited as a DRAFT. Submitting it validates it and raises a
`notification.template_change` request (always a checker, India and the US); the draft sits in
`pending_approval` until a DIFFERENT holder of `approval.check` + `notify.template` in the
template's country approves, when it becomes ACTIVE and the previously active version of the
same (key, channel, country, locale) is ARCHIVED. A rejected, cancelled or expired request
returns the draft to `draft`. Retiring an active template is the same action with kind
`archive`.

THE INDIA SMS RULE
------------------
An India SMS template cannot be activated without a registration for the configured India SMS
provider that carries the DLT template id, the sender id and the provider's flow id, AND the
body must map 1:1 onto the DLT-approved text (`templating.dlt_mapping_problems`). The refusal
names what is missing. It is checked when the maker submits AND again when the checker
approves, because the registration may have changed in between.
"""

import hashlib
import json
import logging

from django.db import transaction
from django.utils import timezone

from servers.admin_access.bridge import granted
from servers.admin_access.models import COUNTRY, GeoNode
from servers.admin_access.scoping import scoped_or_none
from servers.admin_audit.services import record_admin_action
from servers.approvals import registry
from servers.approvals.actions import NOTIFICATION_TEMPLATE_CHANGE
from servers.approvals.service import (
    InvalidRequest, NotInScope, NotPermitted, WrongState, require_approval_or_execute,
)
from base.countries import COUNTRIES

from . import config, templating
from .models import (
    ACTIVE, ARCHIVED, CHANNELS, DRAFT, EMAIL, IN_APP, PENDING_APPROVAL, PROMOTIONAL, PUSH, SMS,
    TRANSACTIONAL, WHATSAPP, NotificationTemplate, TemplateRegistration,
)

logger = logging.getLogger(__name__)

CAPABILITY = 'notify.template'
KIND_ACTIVATE, KIND_ARCHIVE = 'activate', 'archive'
EDITABLE = ('subject', 'title', 'body', 'variables', 'personal_variables', 'sample_values',
            'category', 'reason')
REGISTRATION_FIELDS = ('sender_id', 'dlt_template_id', 'entity_id', 'registered_text',
                       'provider_template_id', 'whatsapp_template_name', 'whatsapp_namespace')


def country_node(code):
    return GeoNode.objects.filter(kind=COUNTRY, code=code).first()


def _need_reason(reason):
    if not (reason or '').strip():
        raise InvalidRequest('A reason is required.', code='REASON_REQUIRED', field='reason')
    return reason.strip()


def _gate(user, template):
    """403 when the template is in scope but `notify.template` is not held at its country."""
    node = template.geo_node
    if node is None or not granted(user, CAPABILITY, node)[0]:
        raise NotPermitted('You do not hold notify.template here.', code='SCOPE_NOT_HELD',
                           field='template')


def template_dict(template, *, with_registrations=True):
    out = {
        'id': template.id, 'key': template.key, 'channel': template.channel,
        'country': template.country, 'locale': template.locale, 'category': template.category,
        'subject': template.subject, 'title': template.title, 'body': template.body,
        'variables': template.variables, 'personal_variables': template.personal_variables,
        'sample_values': template.sample_values, 'version': template.version,
        'status': template.status, 'reason': template.reason,
        'created_by': {'id': template.created_by_id, 'email': template.created_by_email,
                       'label': template.created_by_label, 'role': template.created_by_role},
        'created_at': template.created_at.isoformat(),
        'activated_at': template.activated_at.isoformat() if template.activated_at else None,
        'archived_at': template.archived_at.isoformat() if template.archived_at else None,
        'changed_via_request_id': template.changed_via_id,
    }
    if with_registrations:
        out['registrations'] = [registration_dict(r) for r in template.registrations.all()]
    return out


def registration_dict(reg):
    return {'id': reg.id, 'provider': reg.provider, **{f: getattr(reg, f) for f in REGISTRATION_FIELDS}}


# ---------------------------------------------------------------------------
# Problems (the same list drives the editor's inline refusal and the submit refusal)
# ---------------------------------------------------------------------------

def registration_problems(channel, country, body, registrations):
    """The channel-specific registration rules, over plain dicts (so an UNSAVED editor state
    can be checked too). `registrations` is `[{provider, sender_id, dlt_template_id, ...}]`."""
    problems = []

    def add(code, field, message):
        problems.append({'code': code, 'field': field, 'message': message})

    def reg_for(provider):
        for reg in registrations or []:
            if reg.get('provider') == provider:
                return reg
        return None

    def has(reg, name):
        return bool(reg) and bool(str(reg.get(name) or '').strip())

    if channel == SMS and country == 'IN':
        provider = config.configured_provider(SMS, 'IN') or 'msg91'
        reg = reg_for(provider)
        missing = []
        if not has(reg, 'dlt_template_id'):
            missing.append('DLT template ID')
        if not has(reg, 'sender_id'):
            missing.append('sender ID')
        if not has(reg, 'provider_template_id'):
            missing.append(f'{provider} flow ID')
        if missing:
            add('DLT_REGISTRATION_REQUIRED', 'registrations',
                f'An India SMS template cannot be activated without its {" and ".join(missing)} '
                f'registered for {provider}. Add the registration, then submit again.')
        if has(reg, 'registered_text') or not missing:
            for text in templating.dlt_mapping_problems(body, (reg or {}).get('registered_text')):
                add('DLT_PLACEHOLDER_MISMATCH', 'registrations', text)

    if channel == WHATSAPP:
        provider = config.configured_provider(WHATSAPP, country) or 'msg91'
        if not has(reg_for(provider), 'whatsapp_template_name'):
            add('WHATSAPP_REGISTRATION_REQUIRED', 'registrations',
                f'A WhatsApp template cannot be activated without the approved template name '
                f'registered for {provider}.')
    return problems


def activation_problems(template):
    """Everything stopping `template` from going live, as `[{code, field, message}]`."""
    problems = []

    def add(code, field, message):
        problems.append({'code': code, 'field': field, 'message': message})

    for text in templating.validate(
            body=template.body, subject=template.subject, title=template.title,
            variables=template.variables, personal_variables=template.personal_variables):
        add('TEMPLATE_INVALID', 'body', text)
    if template.channel == EMAIL and not template.subject.strip():
        add('TEMPLATE_INVALID', 'subject', 'An email template needs a subject.')
    if template.channel in (PUSH, IN_APP) and not template.title.strip():
        add('TEMPLATE_INVALID', 'title', 'A push or in-app template needs a title.')
    if template.category == PROMOTIONAL and template.personal_variables:
        add('TEMPLATE_INVALID', 'personal_variables',
            'A promotional template cannot use personal variables.')
    problems += registration_problems(
        template.channel, template.country, template.body,
        [registration_dict(r) for r in template.registrations.all()])
    return problems


def refusal_for(problems):
    """The ApprovalError that carries the first problem's code and every message."""
    first = problems[0]
    return InvalidRequest(
        ' '.join(p['message'] for p in problems), code=first['code'], field=first['field'],
        status=422)


def preview(template_like, values=None):
    """Render with sample values. `template_like` has body/subject/title/variables/samples."""
    values = dict(template_like.sample_values or {}) | dict(values or {})
    out = {'values': values, 'problems': templating.validate(
        body=template_like.body, subject=template_like.subject, title=template_like.title,
        variables=template_like.variables, personal_variables=template_like.personal_variables)}
    try:
        out['body'] = templating.render(template_like.body, values)
        out['subject'] = templating.render(template_like.subject, values) if template_like.subject else ''
        out['title'] = templating.render(template_like.title, values) if template_like.title else ''
        out['missing'] = []
    except templating.MissingVariable as exc:
        out['body'] = out['subject'] = out['title'] = ''
        out['missing'] = exc.names
    return out


# ---------------------------------------------------------------------------
# Drafting
# ---------------------------------------------------------------------------

def _clean_fields(data, template=None):
    out = {}
    for name in EDITABLE:
        if name in data:
            out[name] = data[name]
    for name in ('variables', 'personal_variables'):
        if name in out:
            value = out[name]
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise InvalidRequest(f'{name} must be a list of names.', code='INVALID_FIELD',
                                     field=name)
    if 'sample_values' in out and not isinstance(out['sample_values'], dict):
        raise InvalidRequest('sample_values must be an object.', code='INVALID_FIELD',
                             field='sample_values')
    if 'category' in out and out['category'] not in (TRANSACTIONAL, PROMOTIONAL):
        raise InvalidRequest('category must be transactional or promotional.',
                             code='INVALID_FIELD', field='category')
    for name in ('subject', 'title', 'body', 'reason'):
        if name in out and not isinstance(out[name], str):
            raise InvalidRequest(f'{name} must be text.', code='INVALID_FIELD', field=name)
    return out


def _audit(request, action, template, before=None, reason=''):
    record_admin_action(
        request, action=action, target_type='notification_template', target_id=template.id,
        before=before or {}, after=template_dict(template, with_registrations=False),
        reason=reason or template.reason)


@transaction.atomic
def create_draft(actor, data, *, http_request=None):
    key = str(data.get('key') or '').strip()
    channel = data.get('channel')
    country = str(data.get('country') or '').strip().upper()
    locale = str(data.get('locale') or 'en').strip()
    if locale != 'en':
        raise InvalidRequest('Only English copy exists for now. Hindi and Telugu come later, '
                             'reviewed by a native speaker.', code='LOCALE_NOT_SUPPORTED',
                             field='locale')
    if not key or not all(ch.isalnum() or ch in '._-' for ch in key):
        raise InvalidRequest('A template key like commission.increase_notice is required.',
                             code='INVALID_FIELD', field='key')
    if channel not in CHANNELS:
        raise InvalidRequest('channel must be one of: ' + ', '.join(CHANNELS),
                             code='INVALID_FIELD', field='channel')
    if country not in COUNTRIES:
        raise InvalidRequest('country must be one of: ' + ', '.join(sorted(COUNTRIES))
                             + '. Templates are written per country.',
                             code='COUNTRY_REQUIRED', field='country')
    node = country_node(country)
    if node is None or not granted(actor, CAPABILITY, node)[0]:
        raise NotPermitted('You do not hold notify.template in that country.',
                           code='SCOPE_NOT_HELD', field='country')
    fields = _clean_fields(data)
    reason = _need_reason(fields.get('reason') or data.get('reason'))
    fields['reason'] = reason
    fields.setdefault('body', '')
    last = (NotificationTemplate.objects.filter(key=key, channel=channel, country=country,
                                                locale=locale)
            .order_by('-version').first())
    template = NotificationTemplate(
        key=key, channel=channel, country=country, geo_node=node, locale=locale,
        version=(last.version + 1) if last else 1, status=DRAFT, **fields)
    template.set_created_by(actor)
    template.save()
    if last is not None and data.get('copy_registrations', True):
        for reg in last.registrations.all():
            TemplateRegistration.objects.create(
                template=template, provider=reg.provider,
                **{f: getattr(reg, f) for f in REGISTRATION_FIELDS})
    _audit(http_request, 'notification_template_created', template, reason=reason)
    return template


@transaction.atomic
def update_draft(actor, template, data, *, http_request=None):
    _gate(actor, template)
    template = NotificationTemplate.objects.select_for_update().get(pk=template.pk)
    if template.status != DRAFT:
        raise WrongState('Only a draft can be edited. Create a new version instead.',
                         code='TEMPLATE_NOT_DRAFT', field='status')
    fields = _clean_fields(data)
    before = template_dict(template, with_registrations=False)
    for name, value in fields.items():
        setattr(template, name, value)
    template.save()
    record_admin_action(
        http_request, action='notification_template_updated', target_type='notification_template',
        target_id=template.id, before=before,
        after=template_dict(template, with_registrations=False), reason=template.reason)
    return template


@transaction.atomic
def set_registrations(actor, template, rows, *, http_request=None):
    _gate(actor, template)
    template = NotificationTemplate.objects.select_for_update().get(pk=template.pk)
    if template.status != DRAFT:
        raise WrongState('Registrations can be changed on a draft only.',
                         code='TEMPLATE_NOT_DRAFT', field='status')
    if not isinstance(rows, list):
        raise InvalidRequest('registrations must be a list.', code='INVALID_FIELD',
                             field='registrations')
    before = [registration_dict(r) for r in template.registrations.all()]
    seen = set()
    for row in rows:
        provider = str((row or {}).get('provider') or '').strip()
        if provider not in ('msg91', 'twilio', 'ses', 'fcm') or provider in seen:
            raise InvalidRequest('Each registration needs a distinct, known provider.',
                                 code='INVALID_FIELD', field='provider')
        seen.add(provider)
        values = {f: str((row or {}).get(f) or '').strip() for f in REGISTRATION_FIELDS}
        TemplateRegistration.objects.update_or_create(
            template=template, provider=provider, defaults=values)
    template.registrations.exclude(provider__in=seen).delete()
    after = [registration_dict(r) for r in template.registrations.all()]
    record_admin_action(
        http_request, action='notification_template_registrations_set',
        target_type='notification_template', target_id=template.id,
        before={'registrations': before}, after={'registrations': after}, reason=template.reason)
    return template


# ---------------------------------------------------------------------------
# Submitting
# ---------------------------------------------------------------------------

def _digest(payload):
    raw = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:24]
    return '-'.join(raw[i:i + 6] for i in range(0, 24, 6))


def _raise_request(actor, template, kind, reason, http_request):
    payload = {'kind': kind, 'template_id': template.id, 'key': template.key,
               'channel': template.channel, 'country': template.country,
               'version': template.version, 'category': template.category}
    return require_approval_or_execute(
        NOTIFICATION_TEMPLATE_CHANGE, actor=actor, country=template.country,
        scope_node=template.geo_node, target_type='notification_template',
        target_id=str(template.id), payload=payload,
        idempotency_key=f'notification.template_change:{kind}:{template.id}:{_digest(payload)}',
        reason=reason, http_request=http_request)


def propose_activation(actor, template, *, reason='', http_request=None):
    _gate(actor, template)
    reason = _need_reason(reason)
    with transaction.atomic():
        locked = NotificationTemplate.objects.select_for_update().get(pk=template.pk)
        if locked.status != DRAFT:
            raise WrongState('Only a draft can be submitted for activation.',
                             code='TEMPLATE_NOT_DRAFT', field='status')
        problems = activation_problems(locked)
        if problems:
            raise refusal_for(problems)
        outcome = _raise_request(actor, locked, KIND_ACTIVATE, reason, http_request)
        locked.status = PENDING_APPROVAL
        locked.save(update_fields=['status'])
    _audit(http_request, 'notification_template_submitted', locked, reason=reason)
    return outcome


def propose_archive(actor, template, *, reason='', http_request=None):
    _gate(actor, template)
    reason = _need_reason(reason)
    if template.status != ACTIVE:
        raise WrongState('Only an active template can be retired.', code='TEMPLATE_NOT_ACTIVE',
                         field='status')
    outcome = _raise_request(actor, template, KIND_ARCHIVE, reason, http_request)
    _audit(http_request, 'notification_template_retire_requested', template, reason=reason)
    return outcome


# ---------------------------------------------------------------------------
# The checker's side
# ---------------------------------------------------------------------------

@registry.register(NOTIFICATION_TEMPLATE_CHANGE)
def execute_template_change(request):
    payload = request.payload or {}
    kind = payload.get('kind')
    with transaction.atomic():
        template = (NotificationTemplate.objects.select_for_update()
                    .filter(pk=payload.get('template_id')).first())
        if template is None:
            raise registry.ExecutionRefused('TEMPLATE_GONE', 'The template no longer exists.')
        now = timezone.now()
        reason = request.decision_reason or request.request_reason
        if kind == KIND_ARCHIVE:
            if template.status != ACTIVE:
                raise registry.ExecutionRefused('TEMPLATE_NOT_ACTIVE',
                                                'The template is no longer active.')
            before = template_dict(template, with_registrations=False)
            template.status = ARCHIVED
            template.archived_at = now
            template.changed_via_id = request.pk
            template.save(update_fields=['status', 'archived_at', 'changed_via_id'])
            record_admin_action(
                None, action='notification_template_archived', target_type='notification_template',
                target_id=template.id, before=before,
                after=template_dict(template, with_registrations=False), reason=reason)
            return {'template_id': template.id, 'status': ARCHIVED}
        if kind != KIND_ACTIVATE:
            raise registry.ExecutionRefused('UNKNOWN_KIND', f'Unknown template change {kind!r}.')
        if template.status != PENDING_APPROVAL:
            raise registry.ExecutionRefused('TEMPLATE_NOT_PENDING',
                                            'The template is no longer waiting for approval.')
        problems = activation_problems(template)
        if problems:
            # The world moved since submission (a registration was cleared): refuse, apply nothing.
            raise registry.ExecutionRefused(problems[0]['code'],
                                            ' '.join(p['message'] for p in problems))
        before = template_dict(template, with_registrations=False)
        superseded = list(NotificationTemplate.objects.select_for_update().filter(
            key=template.key, channel=template.channel, country=template.country,
            locale=template.locale, status=ACTIVE).exclude(pk=template.pk))
        for old in superseded:
            old.status = ARCHIVED
            old.archived_at = now
            old.save(update_fields=['status', 'archived_at'])
        template.status = ACTIVE
        template.activated_at = now
        template.changed_via_id = request.pk
        template.save(update_fields=['status', 'activated_at', 'changed_via_id'])
        record_admin_action(
            None, action='notification_template_activated', target_type='notification_template',
            target_id=template.id, before=before,
            after=template_dict(template, with_registrations=False), reason=reason)
    return {'template_id': template.id, 'status': ACTIVE, 'version': template.version,
            'superseded_ids': [t.id for t in superseded]}


@registry.on_closed(NOTIFICATION_TEMPLATE_CHANGE)
def template_request_closed(request, outcome):
    """A draft whose activation was rejected, cancelled or expired goes back to `draft`."""
    template = NotificationTemplate.objects.select_for_update().filter(
        pk=(request.payload or {}).get('template_id')).first()
    if template is not None and template.status == PENDING_APPROVAL:
        template.status = DRAFT
        template.save(update_fields=['status'])


def lookup(user, template_id):
    """The template if it is inside the caller's scope, else None (the caller answers 404)."""
    return scoped_or_none(user, NotificationTemplate.objects.select_related('geo_node'),
                          pk=template_id)


__all__ = [
    'NotInScope', 'activation_problems', 'create_draft', 'execute_template_change', 'lookup',
    'preview', 'propose_activation', 'propose_archive', 'set_registrations', 'template_dict',
    'update_draft',
]
