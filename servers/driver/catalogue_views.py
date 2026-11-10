"""The vehicle catalogue in the console (Configure > Vehicle catalogue, 10 Nov).

  GET   admin/vehicle-catalogue/?country=&type=&q=   entries, active and retired
  POST  admin/vehicle-catalogue/                      {country, vehicle_type, make, model, reason}
  PATCH admin/vehicle-catalogue/<id>/                 {is_active, reason}

Reading needs driver.read; changes need platform.settings and are audited. An entry is never
deleted (vehicles point at it): it is retired.
"""
from django.db import IntegrityError, transaction
from django.db.models import Q
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated

from base.countries import COUNTRIES
from base.utils import error_response, success_response
from servers.admin_access.bridge import ScopedPermission
from servers.admin_audit.services import record_admin_action

from .models import VehicleCatalogueEntry, VehicleType

READ = ScopedPermission('driver.read')
WRITE = ScopedPermission('platform.settings')


def _row(e):
    return {'id': e.id, 'country': e.country, 'vehicle_type': e.vehicle_type, 'make': e.make, 'model': e.model,
            'source': e.source, 'curated': e.curated, 'is_active': e.is_active}


def _bad(field, message, code='INVALID_VALUE'):
    return error_response(code=code, message=message, field=field, issue=message,
                          status=status.HTTP_400_BAD_REQUEST)


@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated, READ])
def catalogue(request):
    if request.method == 'POST':
        if not WRITE.has_permission(request, None):
            return error_response(code='PERMISSION_DENIED', message='You do not hold platform.settings.',
                                  field='general', issue='platform.settings', status=status.HTTP_403_FORBIDDEN)
        d = request.data or {}
        country = str(d.get('country') or '').upper()
        vtype = str(d.get('vehicle_type') or '')
        make = ' '.join(str(d.get('make') or '').split())[:64]
        model = ' '.join(str(d.get('model') or '').split())[:128]
        if country not in COUNTRIES:
            return _bad('country', 'Choose a country.')
        if not VehicleType.objects.filter(type=vtype).exists():
            return _bad('vehicle_type', 'Choose a vehicle type.')
        if not make or not model:
            return _bad('make', 'Enter the make and the model.')
        try:
            with transaction.atomic():
                e = VehicleCatalogueEntry.objects.create(country=country, vehicle_type=vtype, make=make, model=model,
                                                         source='Added in the console', curated=True)
        except IntegrityError:
            return _bad('model', 'That model is already in the catalogue.', 'DUPLICATE')
        record_admin_action(request, action='vehicle_catalogue_added', target_type='vehicle_catalogue',
                            target_id=str(e.id), before=None, after=_row(e),
                            reason=str(d.get('reason') or 'Added'))
        return success_response(_row(e), status.HTTP_201_CREATED)
    q = VehicleCatalogueEntry.objects.all()
    country = (request.query_params.get('country') or '').upper()
    if country:
        q = q.filter(country=country)
    vtype = request.query_params.get('type') or ''
    if vtype:
        q = q.filter(vehicle_type=vtype)
    text = (request.query_params.get('q') or '').strip()
    if text:
        q = q.filter(Q(make__icontains=text) | Q(model__icontains=text))
    rows = list(q.order_by('country', 'vehicle_type', 'make', 'model')[:500])
    return success_response({'results': [_row(e) for e in rows], 'count': q.count(),
                             'types': list(VehicleType.objects.order_by('id').values_list('type', flat=True))},
                            status.HTTP_200_OK)


@api_view(['PATCH'])
@permission_classes([IsAuthenticated, WRITE])
def catalogue_entry(request, entry_id):
    e = VehicleCatalogueEntry.objects.filter(pk=entry_id).first()
    if e is None:
        return error_response(code='NOT_FOUND', message='No such entry.', field='id', issue='not found',
                              status=status.HTTP_404_NOT_FOUND)
    active = (request.data or {}).get('is_active')
    if not isinstance(active, bool):
        return _bad('is_active', 'is_active must be true or false.')
    before = _row(e)
    e.is_active = active
    e.save(update_fields=['is_active'])
    record_admin_action(request, action='vehicle_catalogue_changed', target_type='vehicle_catalogue',
                        target_id=str(e.id), before=before, after=_row(e),
                        reason=str((request.data or {}).get('reason') or ('Restored' if active else 'Retired')))
    return success_response(_row(e), status.HTTP_200_OK)
