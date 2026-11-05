"""Vehicle classes admin.

  GET   /pricing/admin/vehicle-classes/        every class, with coverage in the caller's scope
  POST  /pricing/admin/vehicle-classes/        add a class                 platform.settings
  PATCH /pricing/admin/vehicle-classes/<id>/   change its description      platform.settings

A class is only offered to riders where a live fare card prices it
(`vehicle_views`), so coverage is the number that matters here. A new class
does appear at once in driver onboarding's vehicle list. The code is never
renamed: the apps and stored trips refer to it.
"""

import re

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated

from base.utils import error_response, success_response
from servers.admin_access.bridge import ScopedPermission, granted
from servers.admin_access.scoping import scoped
from servers.admin_audit.services import record_admin_action
from servers.driver.models import Vehicle, VehicleType

from .models import RateCard, ServiceZone

CODE = re.compile(r'^[a-z][a-z0-9_]{1,31}$')
WRITE = 'platform.settings'


def _refuse(code, message, http_status=status.HTTP_400_BAD_REQUEST, field='general'):
    return error_response(code=code, message=message, field=field, issue=message,
                          status=http_status)


def _live_cards(now):
    return RateCard.objects.filter(
        Q(effective_to__isnull=True) | Q(effective_to__gt=now),
        is_active=True, effective_from__lte=now)


def _rows(user):
    now = timezone.now()
    zones = list(scoped(user, ServiceZone.objects.filter(is_active=True))
                 .select_related('geo_node').order_by('name'))
    covered = {}
    for card in _live_cards(now).filter(zone__in=zones).select_related('zone'):
        covered.setdefault(card.vehicle_type_id, {})[card.zone_id] = card.zone
    vehicles = {}
    for vt_id in scoped(user, Vehicle.objects.filter(status='active')).values_list(
            'vehicle_type_id', flat=True):
        vehicles[vt_id] = vehicles.get(vt_id, 0) + 1
    rows = []
    for vt in VehicleType.objects.order_by('id'):
        zs = covered.get(vt.id, {})
        rows.append({
            'id': vt.id, 'code': vt.type, 'description': vt.description or '',
            'zones_priced': len(zs), 'zones_in_scope': len(zones),
            'zones': [{'id': z.id, 'name': z.name, 'city': z.city} for z in zs.values()],
            'zones_missing': [{'id': z.id, 'name': z.name} for z in zones if z.id not in zs],
            'active_vehicles': vehicles.get(vt.id, 0),
        })
    return rows


@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated, ScopedPermission('zone.read')])
def vehicle_classes(request):
    if request.method == 'GET':
        return success_response({
            'results': _rows(request.user),
            'can_edit': granted(request.user, WRITE)[0],
        }, status.HTTP_200_OK)
    if not granted(request.user, WRITE)[0]:
        return _refuse('FORBIDDEN', 'Adding a vehicle class needs platform.settings.',
                       status.HTTP_403_FORBIDDEN)
    code = (request.data.get('code') or '').strip().lower()
    description = (request.data.get('description') or '').strip()
    if not CODE.match(code):
        return _refuse('INVALID', 'Use 2 to 32 lowercase letters, digits or _, starting with '
                                  'a letter (e.g. auto, bike, suv_xl).', field='code')
    if not description:
        return _refuse('INVALID', 'Describe the class; drivers see it when they register.',
                       field='description')
    try:
        with transaction.atomic():
            vt = VehicleType.objects.create(type=code, description=description)
            record_admin_action(request, action='vehicle_class_created',
                                target_type='VehicleType', target_id=str(vt.pk),
                                after={'code': code, 'description': description},
                                reason=description)
    except IntegrityError:
        return _refuse('DUPLICATE', f'A class called {code} already exists.', field='code')
    return success_response({'id': vt.pk, 'code': vt.type, 'description': vt.description},
                            status.HTTP_201_CREATED)


@api_view(['PATCH'])
@permission_classes([IsAuthenticated, ScopedPermission(WRITE)])
def vehicle_class_detail(request, pk):
    vt = VehicleType.objects.filter(pk=pk).first()
    if vt is None:
        return _refuse('NOT_FOUND', 'No vehicle class matches that id.', status.HTTP_404_NOT_FOUND)
    if 'code' in request.data and request.data['code'] != vt.type:
        return _refuse('CODE_FIXED', 'A class code cannot change; apps and trips refer to it.',
                       field='code')
    description = (request.data.get('description') or '').strip()
    if not description:
        return _refuse('INVALID', 'Describe the class.', field='description')
    before = vt.description
    vt.description = description
    vt.save(update_fields=['description'])
    record_admin_action(request, action='vehicle_class_changed', target_type='VehicleType',
                        target_id=str(vt.pk), before={'description': before},
                        after={'description': description}, reason=description)
    return success_response({'id': vt.pk, 'code': vt.type, 'description': vt.description},
                            status.HTTP_200_OK)
