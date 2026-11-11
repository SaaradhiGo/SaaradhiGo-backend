"""A rider or driver who asks to be erased is anonymised, never deleted.

THE RULING (owner, 2026-10-27)
------------------------------
Rider/driver deletion requests (DPDP Act 2023, CCPA) anonymise the user
record instead of deleting it: the row and its id stay so trips, payments
and the audit trail stay valid; every direct identifier is replaced with an
irreversible placeholder; an `account_anonymised` audit row records the
reason and the request reference. Staff are never anonymised or deleted;
they are deactivated.

`servers.auth_user.erasure.anonymise_user` is the one implementation. The
`anonymise_user` management command and the self-service
`/api/v1/auth/me/delete/` endpoint both call it.
"""

from decimal import Decimal
from io import StringIO

import pytest
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.management import call_command
from django.core.management.base import CommandError

from servers.admin_audit.hashchain import verify_chain
from servers.admin_audit.models import AdminAuditLog
from servers.auth_user.erasure import (
    AUDIT_ACTION,
    AnonymisationRefused,
    anonymise_user,
)
from servers.driver.models import Driver, Vehicle, VehicleType
from servers.ride.models import Trip, TripStatus
from servers.rider.models import FavoritePlace

pytestmark = pytest.mark.django_db

User = get_user_model()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _rider(phone='+919700000401', email='rider@saaradhigo.test'):
    return User.objects.create_user(
        phone_number=phone, email=email, password='secret-pw',
        full_name='Ravi Rider', role='rider', gender='male',
        house_no='12', street='MG Road', city='Hyderabad', zip_code='500001',
        emergency_contact='+919700000999', fcm_token='fcm-token',
    )


def _operator(phone='+919700000501', email='ops@saaradhigo.test'):
    return User.objects.create_user(
        phone_number=phone, email=email, full_name='Ops Person', role='admin',
        is_staff=True, ops_role='support', password='x',
    )


def _status(code):
    obj, _ = TripStatus.objects.get_or_create(status_code=code)
    return obj


def _trip_for(rider):
    return Trip.objects.create(
        user_id=rider, status_id=_status('completed'),
        pickup_lat=Decimal('17.4450000'), pickup_long=Decimal('78.3800000'),
        destination_lat=Decimal('17.4550000'), destination_long=Decimal('78.3900000'),
        estimated_fare=Decimal('150.00'), payment_method='cash',
    )


def _local_storage(monkeypatch, tmp_path):
    """Point the document fields at the filesystem for this test.

    Both fields are declared on S3 storage; the suite has no bucket, and the
    point here is that the files go away, not where they lived.
    """
    from django.core.files.storage import FileSystemStorage

    storage = FileSystemStorage(location=str(tmp_path))
    for model, name in ((Driver, 'license_doc'), (Driver, 'license_doc_back'),
                        (Vehicle, 'rc_doc')):
        monkeypatch.setattr(model._meta.get_field(name), 'storage', storage)
    return storage


def _driver_user(phone='+919700000601'):
    user = User.objects.create_user(
        phone_number=phone, email='drv@saaradhigo.test', full_name='Dev Driver',
        role='driver', password='x',
    )
    driver = Driver.objects.create(user_id=user, approved=True, status='off',
                                   upi_id='dev@upi')
    driver.license_doc.save('lic.jpg', ContentFile(b'jpegbytes'), save=False)
    driver.save()
    vt, _ = VehicleType.objects.get_or_create(type='sedan')
    vehicle = Vehicle.objects.create(driver_id=driver, vehicle_type_id=vt,
                                     vehicle_number='TS09AN0001')
    vehicle.rc_doc.save('rc.pdf', ContentFile(b'pdfbytes'), save=True)
    return user, driver, vehicle


def _anonymise(user, **kwargs):
    kwargs.setdefault('reason', 'DPDP erasure request by email')
    kwargs.setdefault('reference', 'DSR-2026-0142')
    return anonymise_user(user, **kwargs)


# ---------------------------------------------------------------------------
# The service
# ---------------------------------------------------------------------------

class TestWhatIsKept:
    def test_the_row_and_its_pk_survive(self):
        rider = _rider()
        pk = rider.pk

        _anonymise(rider)

        assert User.objects.filter(pk=pk).exists()

    def test_trips_still_point_at_the_user(self):
        rider = _rider()
        trip = _trip_for(rider)

        _anonymise(rider)

        trip.refresh_from_db()
        assert trip.user_id_id == rider.pk
        assert Trip.objects.filter(user_id=rider).count() == 1

    def test_the_role_and_created_at_are_kept(self):
        rider = _rider()
        created = rider.created_at

        _anonymise(rider)

        fresh = User.objects.get(pk=rider.pk)
        assert fresh.role == 'rider'
        assert fresh.created_at == created


class TestWhatIsWiped:
    def test_every_direct_identifier_is_replaced(self):
        rider = _rider()
        pk = rider.pk

        _anonymise(rider)

        fresh = User.objects.get(pk=pk)
        assert fresh.phone_number == f'deleted-{pk}'
        assert fresh.username == f'deleted-{pk}'
        assert fresh.email is None
        assert fresh.full_name == ''
        assert fresh.gender is None
        assert fresh.dob is None
        assert (fresh.house_no, fresh.street, fresh.city, fresh.zip_code) == (None,) * 4
        assert fresh.emergency_contact is None
        assert fresh.fcm_token is None
        assert not fresh.avatar
        assert fresh.email_verified_at is None

    def test_the_account_is_deactivated_and_cannot_sign_in(self):
        rider = _rider()

        _anonymise(rider)

        fresh = User.objects.get(pk=rider.pk)
        assert fresh.is_active is False
        assert not fresh.has_usable_password()
        assert not fresh.check_password('secret-pw')

    def test_refresh_tokens_are_revoked(self):
        from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken
        from rest_framework_simplejwt.tokens import RefreshToken

        rider = _rider()
        RefreshToken.for_user(rider)
        RefreshToken.for_user(rider)

        row = _anonymise(rider)

        assert BlacklistedToken.objects.filter(token__user=rider).count() == 2
        assert row.after['refresh_tokens_revoked'] == 2

    def test_the_placeholder_is_unique_per_account(self):
        first, second = _rider(), _rider(phone='+919700000402', email='b@saaradhigo.test')

        _anonymise(first)
        _anonymise(second)

        assert User.objects.get(pk=first.pk).phone_number != \
            User.objects.get(pk=second.pk).phone_number

    def test_a_drivers_licence_images_payout_id_and_vehicle_papers_go(
            self, monkeypatch, tmp_path):
        storage = _local_storage(monkeypatch, tmp_path)
        user, driver, vehicle = _driver_user()
        licence_name, rc_name = driver.license_doc.name, vehicle.rc_doc.name
        assert storage.exists(licence_name) and storage.exists(rc_name)

        row = _anonymise(user)

        driver.refresh_from_db()
        vehicle.refresh_from_db()
        assert not driver.license_doc
        assert driver.upi_id is None
        assert not vehicle.rc_doc
        assert not storage.exists(licence_name) and not storage.exists(rc_name)
        assert vehicle.vehicle_number == 'TS09AN0001'  # the vehicle, not the person
        assert 'driver.license_doc' in row.after['fields_cleared']
        assert 'driver.upi_id' in row.after['fields_cleared']
        assert f'vehicle[{vehicle.pk}].rc_doc' in row.after['fields_cleared']
        assert row.after['storage_failures'] == []

    def test_a_riders_saved_places_are_removed(self):
        rider = _rider()
        FavoritePlace.objects.create(user_id=rider, address_text='Home, 12 MG Road',
                                     latitude=Decimal('17.4'), longitude=Decimal('78.4'))

        row = _anonymise(rider)

        assert not FavoritePlace.objects.filter(user_id=rider).exists()
        assert 'favorite_places(1)' in row.after['fields_cleared']


class TestTheAuditRow:
    def test_it_records_the_reason_and_the_reference(self):
        rider = _rider()
        operator = _operator()

        row = _anonymise(rider, actor=operator, reason='Erasure request, verified by call',
                         reference='DSR-77')

        assert row.action == AUDIT_ACTION
        assert (row.target_type, row.target_id) == ('User', str(rider.pk))
        assert row.reason == 'Erasure request, verified by call'
        assert row.after['reference'] == 'DSR-77'
        assert row.after['anonymised'] is True
        assert row.before['is_active'] is True
        assert row.actor_id == operator.pk
        assert row.actor_email == 'ops@saaradhigo.test'
        assert row.actor_role == 'admin/support'

    def test_a_self_service_request_names_the_person_as_they_were(self):
        """The actor IS the target. The snapshot must be taken before the
        wipe, or the row would say `deleted-17` asked to be deleted."""
        rider = _rider()

        row = _anonymise(rider, actor=rider)

        assert row.actor_id == rider.pk
        assert row.actor_email == 'rider@saaradhigo.test'
        assert row.actor_label == 'Ravi Rider'
        assert row.actor_role == 'rider'

    def test_rows_the_person_wrote_earlier_keep_their_identifiers(self):
        rider = _rider()
        earlier = AdminAuditLog.objects.create(
            actor=rider, action='dpdp_export', target_type='User',
            target_id=str(rider.pk),
        )

        _anonymise(rider, actor=rider)

        fresh = AdminAuditLog.objects.get(pk=earlier.pk)
        assert fresh.actor_email == 'rider@saaradhigo.test'
        assert fresh.actor_label == 'Ravi Rider'
        assert verify_chain(AdminAuditLog.objects.all())[1] == []

    def test_an_anonymised_actor_still_cannot_be_deleted(self):
        from servers.auth_user.erasure import AccountDeletionRefused

        rider = _rider()
        _anonymise(rider, actor=rider)

        with pytest.raises(AccountDeletionRefused):
            User.objects.get(pk=rider.pk).delete()

    def test_the_chain_verifies_afterwards(self):
        rider = _rider()

        _anonymise(rider, actor=_operator())

        assert verify_chain(AdminAuditLog.objects.all())[1] == []


class TestRefusals:
    def test_an_operator_is_refused_and_untouched(self):
        operator = _operator()

        with pytest.raises(AnonymisationRefused, match='deactivate'):
            _anonymise(operator)

        fresh = User.objects.get(pk=operator.pk)
        assert fresh.email == 'ops@saaradhigo.test'
        assert fresh.is_active is True
        assert not AdminAuditLog.objects.filter(action=AUDIT_ACTION).exists()

    def test_a_superuser_is_refused(self):
        boss = User.objects.create_user(phone_number='+919700000502',
                                        role='rider', is_superuser=True)

        with pytest.raises(AnonymisationRefused):
            _anonymise(boss)

    def test_a_staff_flag_alone_is_enough_to_refuse(self):
        staff = User.objects.create_user(phone_number='+919700000503',
                                         role='rider', is_staff=True)

        with pytest.raises(AnonymisationRefused):
            _anonymise(staff)

    def test_no_reason_is_refused(self):
        rider = _rider()

        with pytest.raises(AnonymisationRefused, match='reason'):
            _anonymise(rider, reason='   ')

        assert User.objects.get(pk=rider.pk).email == 'rider@saaradhigo.test'

    def test_no_reference_is_refused(self):
        rider = _rider()

        with pytest.raises(AnonymisationRefused, match='reference'):
            _anonymise(rider, reference='')


# ---------------------------------------------------------------------------
# The management command
# ---------------------------------------------------------------------------

class TestTheCommand:
    def _run(self, *args):
        out, err = StringIO(), StringIO()
        call_command('anonymise_user', *args, stdout=out, stderr=err)
        return out.getvalue(), err.getvalue()

    def test_by_phone_with_confirm(self):
        rider = _rider()

        out, _ = self._run('--phone', '+919700000401', '--reason', 'DPDP request',
                           '--reference', 'DSR-1', '--confirm')

        assert f'Anonymised user id {rider.pk}' in out
        fresh = User.objects.get(pk=rider.pk)
        assert fresh.phone_number == f'deleted-{rider.pk}'
        row = AdminAuditLog.objects.get(action=AUDIT_ACTION)
        assert row.after['reference'] == 'DSR-1'
        assert row.actor_id is None
        assert row.actor_label == 'anonymise_user (management command)'

    def test_by_email(self):
        rider = _rider()

        self._run('--email', 'Rider@SaaradhiGo.test', '--reason', 'r',
                  '--reference', 'DSR-2', '--confirm')

        assert User.objects.get(pk=rider.pk).email is None

    def test_without_confirm_nothing_changes(self):
        rider = _rider()

        out, _ = self._run('--phone', '+919700000401', '--reason', 'r',
                           '--reference', 'DSR-3')

        assert 'Would anonymise' in out
        assert User.objects.get(pk=rider.pk).email == 'rider@saaradhigo.test'
        assert not AdminAuditLog.objects.filter(action=AUDIT_ACTION).exists()

    def test_actor_email_attributes_the_row_to_an_operator(self):
        rider = _rider()
        operator = _operator()

        self._run('--phone', '+919700000401', '--reason', 'r', '--reference', 'DSR-4',
                  '--actor-email', 'ops@saaradhigo.test', '--confirm')

        row = AdminAuditLog.objects.get(action=AUDIT_ACTION, target_id=str(rider.pk))
        assert row.actor_id == operator.pk
        assert row.actor_label == 'Ops Person'

    def test_actor_email_that_is_not_an_operator_is_refused(self):
        _rider()
        _rider(phone='+919700000402', email='other@saaradhigo.test')

        with pytest.raises(CommandError, match='does not name an operator'):
            self._run('--phone', '+919700000401', '--reason', 'r', '--reference', 'DSR-5',
                      '--actor-email', 'other@saaradhigo.test', '--confirm')

    def test_an_operator_target_is_refused(self):
        operator = _operator()

        with pytest.raises(CommandError, match='never anonymised'):
            self._run('--phone', '+919700000501', '--reason', 'r',
                      '--reference', 'DSR-6', '--confirm')

        assert User.objects.get(pk=operator.pk).email == 'ops@saaradhigo.test'

    def test_a_blank_reason_is_refused(self):
        _rider()

        with pytest.raises(CommandError, match='reason'):
            self._run('--phone', '+919700000401', '--reason', '  ',
                      '--reference', 'DSR-7', '--confirm')

    def test_the_reason_flag_is_mandatory(self):
        _rider()

        with pytest.raises(CommandError):
            self._run('--phone', '+919700000401', '--reference', 'DSR-8', '--confirm')

    def test_an_unknown_account_is_an_error(self):
        with pytest.raises(CommandError, match='No account'):
            self._run('--phone', '+910000000000', '--reason', 'r',
                      '--reference', 'DSR-9', '--confirm')


# ---------------------------------------------------------------------------
# The self-service endpoint runs the same service
# ---------------------------------------------------------------------------

class TestTheSelfServiceEndpoint:
    def test_me_delete_writes_the_anonymised_row_naming_the_person(self, auth_client_rider):
        client, user = auth_client_rider
        email_before = user.email

        resp = client.post('/api/v1/auth/me/delete/',
                           {'confirm': 'DELETE', 'reason': 'leaving'}, format='json')

        assert resp.status_code == 200, resp.content
        row = AdminAuditLog.objects.get(action=AUDIT_ACTION, target_id=str(user.pk))
        assert row.actor_id == user.pk
        assert row.actor_email == (email_before or '')
        assert row.reason == 'leaving'
        assert row.after['reference'] == f'self-service user {user.pk}'
        user.refresh_from_db()
        assert user.is_active is False

    def test_an_operator_cannot_erase_themselves_through_self_service(self, auth_client_admin):
        client, admin = auth_client_admin

        resp = client.post('/api/v1/auth/me/delete/', {'confirm': 'DELETE'}, format='json')

        assert resp.status_code == 403, resp.content
        admin.refresh_from_db()
        assert admin.is_active is True
