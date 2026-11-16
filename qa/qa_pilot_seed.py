# Runs in `manage.py shell` on QA via qrun.sh. Launch round 4 load test (16 Nov): 200 simulated
# drivers and 50 riders, all named "LT1116 ..." with phones +917000100xxx / +917000200xxx.
# Prints one LTTOK line per account; the caller writes them to a mode-600 file, never to screen.
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import transaction
from rest_framework_simplejwt.tokens import AccessToken

from servers.admin_access.models import GeoNode
from servers.driver.models import Driver, Vehicle, VehicleType
from servers.rider.models import Rider

U = get_user_model()
N_DRIVERS, N_RIDERS = 200, 50
hyd = GeoNode.objects.get(code='IN-TG-HYD')
sedan = VehicleType.objects.get(type='sedan')


def token(u):
    t = AccessToken.for_user(u)
    t.set_exp(lifetime=timedelta(hours=3))
    return str(t)


with transaction.atomic():
    for i in range(N_DRIVERS):
        phone = f'+91700010{i:04d}'
        u, _ = U.objects.get_or_create(phone_number=phone, defaults={
            'username': phone, 'role': 'driver', 'full_name': f'LT1116 Driver {i:03d}'})
        d, _ = Driver.objects.get_or_create(user_id=u, defaults={
            'qa_simulated_eligibility': True, 'approved': False, 'is_seed': True,
            'status': 'offline', 'home_city': hyd})
        if d.active_vehicle_id is None:
            v = Vehicle.objects.create(driver_id=d, vehicle_type_id=sedan,
                                       vehicle_number=f'TS16LT{i:04d}', brand='Maruti',
                                       model='Dzire', color='white', year=2022, capacity=4)
            d.active_vehicle = v
            d.save(update_fields=['active_vehicle'])
        print(f'LTTOK d {i} {u.id} {d.id} {token(u)}')
    for i in range(N_RIDERS):
        phone = f'+91700020{i:04d}'
        u, _ = U.objects.get_or_create(phone_number=phone, defaults={
            'username': phone, 'role': 'rider', 'full_name': f'LT1116 Rider {i:03d}'})
        Rider.objects.get_or_create(user_id=u)
        print(f'LTTOK r {i} {u.id} 0 {token(u)}')
print('LTDONE', U.objects.filter(full_name__startswith='LT1116').count())
