"""`enroll_operator_mfa --replace` proves the new factor before the old one goes.

Owner ruling, 2026-10-28: "MFA --replace: confirm the new factor before
removing the old; audit both; never leave an account with zero factors
(when MFA is required)."

The failure this closes: `--replace` deleted every device first and created
the new one second, so between the two runs -- or for ever, if the operator
never scanned the new secret -- the account had no factor. In an enforced
environment that is a locked-out operator; in an unenforced one it is an
operator account with MFA silently off.
"""

import time
from io import StringIO

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django_otp.oath import TOTP
from django_otp.plugins.otp_static.models import StaticDevice, StaticToken
from django_otp.plugins.otp_totp.models import TOTPDevice

from base import ops_mfa
from servers.admin_audit.models import AdminAuditLog
from servers.auth_user.management.commands.enroll_operator_mfa import (
    ENROLLED_ACTION,
    REPLACED_ACTION,
)

pytestmark = pytest.mark.django_db

User = get_user_model()

PHONE = '+919600000601'
ACTOR_EMAIL = 'ops.lead@saaradhigo.test'


def _code(device):
    totp = TOTP(device.bin_key, device.step, device.t0, device.digits, device.drift)
    totp.time = time.time()
    return totp.token()


@pytest.fixture
def actor():
    return User.objects.create_user(phone_number='+919600000001', email=ACTOR_EMAIL,
                                    role='admin', is_staff=True)


@pytest.fixture
def enrolled(actor):
    """An operator with one confirmed TOTP device and a set of recovery codes."""
    user = User.objects.create_user(phone_number=PHONE, email='op@saaradhigo.test',
                                    role='admin', is_staff=True)
    device, _uri = ops_mfa.enroll_totp(user)
    device.confirmed = True
    device.save(update_fields=['confirmed'])
    codes = ops_mfa.issue_recovery_codes(user, count=3)
    return user, device, codes


def _run(*args):
    out = StringIO()
    call_command('enroll_operator_mfa', '--phone', PHONE, '--actor-email', ACTOR_EMAIL,
                 *args, stdout=out)
    return out.getvalue()


def _confirmed_totp(user):
    return list(TOTPDevice.objects.filter(user=user, confirmed=True).order_by('id'))


class TestTheOldFactorSurvivesUntilTheNewOneIsProved:
    def test_without_replace_an_enrolled_account_is_refused_and_untouched(self, enrolled):
        user, old, codes = enrolled

        with pytest.raises(CommandError, match='--replace'):
            _run()

        assert _confirmed_totp(user) == [old]
        assert StaticToken.objects.filter(device__user=user).count() == 3

    def test_the_start_run_adds_a_pending_device_and_removes_nothing(self, enrolled):
        user, old, codes = enrolled

        out = _run('--replace')

        assert _confirmed_totp(user) == [old]
        assert ops_mfa.pending_totp_device(user) is not None
        assert ops_mfa.pending_totp_device(user).pk != old.pk
        assert StaticToken.objects.filter(device__user=user).count() == 3
        assert 'still work' in out
        assert ops_mfa.verify_code(user, _code(old)) is True
        assert AdminAuditLog.objects.filter(action__in=[ENROLLED_ACTION, REPLACED_ACTION]).count() == 0

    def test_a_wrong_code_removes_nothing_and_records_nothing(self, enrolled):
        user, old, codes = enrolled
        _run('--replace')
        before = AdminAuditLog.objects.count()

        with pytest.raises(CommandError, match='did not verify'):
            _run('--replace', '--code', '000000')

        assert _confirmed_totp(user) == [old]
        assert ops_mfa.pending_totp_device(user) is not None, 'the pending device is kept for a retry'
        assert StaticToken.objects.filter(device__user=user).count() == 3
        assert AdminAuditLog.objects.count() == before
        assert ops_mfa.has_device(user)

    def test_the_right_code_confirms_the_new_device_then_removes_the_old(self, enrolled, actor):
        user, old, codes = enrolled
        _run('--replace')
        new = ops_mfa.pending_totp_device(user)

        out = _run('--replace', '--code', _code(new))

        assert _confirmed_totp(user) == [new]
        assert not TOTPDevice.objects.filter(pk=old.pk).exists()
        assert ops_mfa.pending_totp_device(user) is None
        # Recovery codes reissued: the old three are gone, a fresh ten exist.
        tokens = set(StaticToken.objects.filter(device__user=user).values_list('token', flat=True))
        assert len(tokens) == ops_mfa.RECOVERY_CODE_COUNT
        assert tokens.isdisjoint(codes)
        assert 'Removed 2 previous factor' in out   # the old TOTP entry and its recovery set
        assert ops_mfa.has_device(user)

    def test_both_halves_are_audited_with_the_actor(self, enrolled, actor):
        user, old, codes = enrolled
        _run('--replace')
        new = ops_mfa.pending_totp_device(user)

        _run('--replace', '--code', _code(new))

        enrolled_row = AdminAuditLog.objects.get(action=ENROLLED_ACTION)
        replaced_row = AdminAuditLog.objects.get(action=REPLACED_ACTION)
        for row in (enrolled_row, replaced_row):
            assert row.actor_id == actor.pk
            assert row.actor_label == f'{ACTOR_EMAIL} via enroll_operator_mfa'
            assert row.target_type == 'operator'
            assert row.target_id == str(user.pk)
            blob = f'{row.before}{row.after}{row.reason}'
            assert new.key not in blob and old.key not in blob
            assert PHONE not in blob
        assert enrolled_row.after['replaced_existing'] is True
        assert enrolled_row.after['recovery_codes_issued'] == ops_mfa.RECOVERY_CODE_COUNT
        assert replaced_row.before == {'confirmed_factors': 2}   # old TOTP + recovery set
        assert replaced_row.after['devices_removed'] >= 1
        assert replaced_row.after['confirmed_factors'] >= 1
        assert enrolled_row.pk < replaced_row.pk

    def test_at_no_point_between_the_two_runs_is_the_account_factorless(self, enrolled, settings):
        settings.OPS_MFA_ENFORCED = True
        # django-otp throttles a device after a failed code; the retry below
        # is immediate, so the throttle is off for this test only.
        settings.OTP_TOTP_THROTTLE_FACTOR = 0
        user, old, codes = enrolled
        assert ops_mfa.mfa_required_for(user)

        _run('--replace')
        assert ops_mfa.has_device(user)
        with pytest.raises(CommandError):
            _run('--replace', '--code', '111111')
        assert ops_mfa.has_device(user)
        _run('--replace', '--code', _code(ops_mfa.pending_totp_device(user)))
        assert ops_mfa.has_device(user)


class TestTheZeroFactorInvariantIsEnforcedNotAssumed:
    def test_a_removal_that_would_strip_every_factor_is_refused_and_rolled_back(
            self, enrolled, monkeypatch, settings):
        """Simulate the one bug the invariant exists for: the removal takes
        the new device with it. The command must refuse and roll back, so the
        OLD factor is still there afterwards."""
        settings.OPS_MFA_ENFORCED = True
        user, old, codes = enrolled
        _run('--replace')
        new = ops_mfa.pending_totp_device(user)

        def remove_everything(user_, keep=None):
            return ops_mfa.remove_devices(user_)

        monkeypatch.setattr(ops_mfa, 'remove_other_devices', remove_everything)

        with pytest.raises(CommandError, match='no confirmed factor'):
            _run('--replace', '--code', _code(new))

        assert TOTPDevice.objects.filter(pk=old.pk, confirmed=True).exists(), (
            'the removal was not rolled back')
        assert ops_mfa.has_device(user)
        assert AdminAuditLog.objects.filter(action__in=[ENROLLED_ACTION, REPLACED_ACTION]).count() == 0

    def test_first_enrolment_on_a_bare_account_still_works(self, actor):
        user = User.objects.create_user(phone_number=PHONE, email='op@saaradhigo.test',
                                        role='admin', is_staff=True)

        _run()
        _run('--code', _code(ops_mfa.pending_totp_device(user)))

        assert len(_confirmed_totp(user)) == 1
        assert AdminAuditLog.objects.filter(action=ENROLLED_ACTION).count() == 1
        assert not AdminAuditLog.objects.filter(action=REPLACED_ACTION).exists()
        assert not StaticDevice.objects.filter(user=user, confirmed=False).exists()


class TestRemoveOtherDevices:
    def test_keeps_exactly_the_named_device(self, enrolled):
        user, old, codes = enrolled
        new, _ = ops_mfa.enroll_totp(user)
        new.confirmed = True
        new.save(update_fields=['confirmed'])

        removed = ops_mfa.remove_other_devices(user, keep=new)

        assert removed >= 2   # the old TOTP device and the recovery set
        assert list(TOTPDevice.objects.filter(user=user)) == [new]
        assert not StaticDevice.objects.filter(user=user).exists()

    def test_does_not_touch_another_account(self, enrolled):
        user, old, codes = enrolled
        other = User.objects.create_user(phone_number='+919600000777', role='admin',
                                         is_staff=True)
        theirs, _ = ops_mfa.enroll_totp(other)
        new, _ = ops_mfa.enroll_totp(user)

        ops_mfa.remove_other_devices(user, keep=new)

        assert TOTPDevice.objects.filter(pk=theirs.pk).exists()
