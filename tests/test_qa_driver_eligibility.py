"""Simulated QA eligibility must never become a real approval, anywhere.

The thing being prevented: a QA test driver, whose "documents" are
placeholder images, being recorded as having passed a real MVA-2020 KYC
review -- or being dispatchable anywhere other than QA.

The tests below are the boundary. If any of them fails, the separation
between "we are pretending in QA" and "a human verified real documents" has
been lost, and that is not a cosmetic regression.
"""

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from servers.driver.models import Driver, Vehicle, VehicleType
from servers.driver.qa_eligibility import (
    eligibility_label,
    is_qa_test_driver,
    may_operate,
    qa_simulation_allowed,
)

User = get_user_model()
pytestmark = pytest.mark.django_db

QA = dict(ENVIRONMENT='qa', IS_PRODUCTION=False)
PROD = dict(ENVIRONMENT='production', IS_PRODUCTION=True)


def _driver(phone='+917770001003', with_vehicle=True, **kw):
    user = User.objects.create_user(phone_number=phone, role='driver')
    driver = Driver.objects.create(user_id=user, **kw)
    if with_vehicle:
        vtype, _ = VehicleType.objects.get_or_create(type='sedan')
        # NB: this repo names its FKs with an `_id` suffix -- the field is
        # `vehicle_type_id` and it holds the VehicleType instance, not a pk.
        vehicle = Vehicle.objects.create(
            driver_id=driver, vehicle_type_id=vtype, vehicle_number='AP16QA0001',
        )
        driver.active_vehicle = vehicle
        driver.save(update_fields=['active_vehicle'])
    return driver


# --------------------------------------------------------------------------
# the environment gate
# --------------------------------------------------------------------------

@override_settings(**QA)
def test_simulation_allowed_only_in_qa():
    assert qa_simulation_allowed() is True


@override_settings(**PROD)
def test_simulation_refused_in_production():
    assert qa_simulation_allowed() is False


@pytest.mark.parametrize('environment', ['production', 'development', 'load', 'staging', ''])
def test_simulation_refused_in_every_other_environment(environment):
    with override_settings(ENVIRONMENT=environment, IS_PRODUCTION=False):
        assert qa_simulation_allowed() is False, (
            f'{environment!r} must not honour simulated eligibility; only an '
            f'explicit "qa" may'
        )


@override_settings(ENVIRONMENT='qa', IS_PRODUCTION=True)
def test_disagreement_between_the_two_settings_is_refused():
    """ENVIRONMENT says qa, the production flag says production: refuse."""
    assert qa_simulation_allowed() is False


# --------------------------------------------------------------------------
# may_operate
# --------------------------------------------------------------------------

@override_settings(**QA)
def test_simulated_driver_may_operate_in_qa():
    driver = _driver(qa_simulated_eligibility=True)
    assert driver.approved is False
    assert may_operate(driver) is True


@override_settings(**PROD)
def test_simulated_driver_may_not_operate_in_production():
    """The whole point. Same row, production: dispatch must refuse it."""
    driver = _driver(qa_simulated_eligibility=True)
    assert may_operate(driver) is False


@override_settings(**QA)
def test_unflagged_unapproved_driver_may_not_operate():
    assert may_operate(_driver()) is False


@override_settings(**PROD)
def test_genuinely_approved_driver_may_operate_in_production():
    """Simulation must not have narrowed the real path."""
    assert may_operate(_driver(approved=True)) is True


def test_may_operate_handles_none():
    assert may_operate(None) is False


# --------------------------------------------------------------------------
# the flag is never an approval
# --------------------------------------------------------------------------

@override_settings(**QA)
def test_simulated_eligibility_does_not_touch_approval_fields():
    driver = _driver()
    call_command('qa_simulate_driver_eligibility', '--driver-id', str(driver.id))
    driver.refresh_from_db()

    assert driver.qa_simulated_eligibility is True
    assert driver.approved is False, 'a QA flag must never set approved'
    assert driver.doc_status == 'pending', (
        'doc_status must keep saying pending: the documents really are '
        'unverified placeholders'
    )


@override_settings(**QA)
def test_label_distinguishes_simulated_from_approved():
    assert eligibility_label(_driver(qa_simulated_eligibility=True)) == 'qa-simulated'
    assert eligibility_label(_driver(phone='+917770001004', approved=True)) == 'approved'
    assert eligibility_label(_driver(phone='+917770001005')) == 'not-eligible'


@override_settings(**PROD)
def test_test_driver_is_identifiable_even_where_simulation_is_ignored():
    """The console must be able to label it anywhere, including production."""
    driver = _driver(qa_simulated_eligibility=True)
    assert is_qa_test_driver(driver) is True
    assert may_operate(driver) is False


# --------------------------------------------------------------------------
# the command's own guards
# --------------------------------------------------------------------------

@override_settings(**PROD)
def test_command_refuses_in_production():
    driver = _driver()
    with pytest.raises(CommandError, match='refusing to run'):
        call_command('qa_simulate_driver_eligibility', '--driver-id', str(driver.id))
    driver.refresh_from_db()
    assert driver.qa_simulated_eligibility is False


@pytest.mark.parametrize('environment', ['production', 'development', 'load', ''])
def test_command_refuses_outside_qa(environment):
    with override_settings(ENVIRONMENT=environment, IS_PRODUCTION=False):
        driver = _driver()
        with pytest.raises(CommandError, match='refusing to run'):
            call_command('qa_simulate_driver_eligibility', '--driver-id', str(driver.id))


@override_settings(**QA)
def test_command_refuses_a_number_outside_the_test_range():
    """Even in QA it cannot touch something that might be a real driver."""
    driver = _driver(phone='+919876543210')
    with pytest.raises(CommandError, match='not a QA test account'):
        call_command('qa_simulate_driver_eligibility', '--driver-id', str(driver.id))
    driver.refresh_from_db()
    assert driver.qa_simulated_eligibility is False


@override_settings(**QA)
def test_command_refuses_a_driver_with_no_active_vehicle():
    driver = _driver(with_vehicle=False)
    with pytest.raises(CommandError, match='no active vehicle'):
        call_command('qa_simulate_driver_eligibility', '--driver-id', str(driver.id))


@override_settings(**QA)
def test_command_is_reversible():
    driver = _driver()
    call_command('qa_simulate_driver_eligibility', '--driver-id', str(driver.id))
    call_command('qa_simulate_driver_eligibility', '--driver-id', str(driver.id), '--revert')
    driver.refresh_from_db()
    assert driver.qa_simulated_eligibility is False


@override_settings(**QA)
def test_command_writes_an_audit_row_saying_it_is_not_an_approval():
    from servers.admin_audit.models import AdminAuditLog

    driver = _driver()
    call_command('qa_simulate_driver_eligibility', '--driver-id', str(driver.id))

    row = AdminAuditLog.objects.get(action='qa_simulated_driver_eligibility')
    assert row.target_id == str(driver.id)
    assert 'NOT' in row.reason and 'KYC approval' in row.reason
    assert row.before['approved'] is False
    assert row.after['approved'] is False, (
        'the audit trail must record that approval was never granted'
    )


@override_settings(**QA)
def test_command_refuses_an_unknown_driver():
    with pytest.raises(CommandError, match='no driver with id'):
        call_command('qa_simulate_driver_eligibility', '--driver-id', '999999')
