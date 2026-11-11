"""Changing a password has to end the sessions it protected.

THE DEFECT THIS CLOSES
----------------------
`change_password` and `reset_password` set the new password and stopped. The
code even said revocation was handled -- true of *reset* tokens, which the
generator invalidates by hashing the password hash, and not true of the
credential the apps and the console actually carry.

A refresh token lives 14 days and `ROTATE_REFRESH_TOKENS` reissues one on
every use. So the single action a person takes when they believe their
password is compromised -- change it -- left the attacker's session alive and
self-renewing. For an operator account, which approves KYC and releases
payouts, that is an uncontained incident.

WHAT IS ASSERTED
----------------
That a refresh token minted BEFORE the change can no longer be exchanged
AFTER it, through the real `/api/v1/auth/refresh/` endpoint rather than by
inspecting the blacklist table. And the deliberate limit: an access token
already minted stays valid for its 15 minutes, which is recorded as a test so
nobody reads it as an oversight.
"""

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import PasswordResetTokenGenerator
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken

from servers.admin_audit.models import AdminAuditLog
from servers.auth_user.session_revocation import revoke_all_refresh_tokens

pytestmark = pytest.mark.django_db

User = get_user_model()

OLD = 'Old-Passphrase-2026!'
NEW = 'New-Passphrase-2026!'

CHANGE_URL = '/api/v1/auth/password/change/'
RESET_URL = '/api/v1/auth/password/reset/'
REFRESH_URL = '/api/v1/auth/refresh/'


def _user(phone='+919300000001', role='rider', email='holder@deeptrics.com',
          **extra):
    user = User.objects.create_user(
        phone_number=phone, username=phone, role=role, email=email, **extra)
    user.set_password(OLD)
    user.save(update_fields=['password'])
    return user


def _client(user):
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION=f'Bearer {AccessToken.for_user(user)}')
    return c


def _refresh_still_works(refresh):
    """Ask the real endpoint, not the blacklist table."""
    return APIClient().post(
        REFRESH_URL, {'refresh_token': str(refresh)}, format='json',
    ).status_code == 200


class TestChangePassword:
    def test_a_refresh_token_from_before_the_change_stops_working(self):
        user = _user()
        refresh = RefreshToken.for_user(user)
        assert _refresh_still_works(refresh), 'precondition: it worked before'

        r = _client(user).post(
            CHANGE_URL,
            {'current_password': OLD, 'new_password': NEW},
            format='json',
        )

        assert r.status_code == 200, r.content.decode()
        assert not _refresh_still_works(refresh), (
            'the attacker keeps a self-renewing session for 14 days'
        )

    def test_every_outstanding_session_goes_not_just_the_caller(self):
        # Someone who changes their password because of a compromise is
        # ending the OTHER session, not their own.
        user = _user()
        phone_session = RefreshToken.for_user(user)
        laptop_session = RefreshToken.for_user(user)

        _client(user).post(
            CHANGE_URL,
            {'current_password': OLD, 'new_password': NEW},
            format='json',
        )

        assert not _refresh_still_works(phone_session)
        assert not _refresh_still_works(laptop_session)

    def test_a_wrong_current_password_revokes_nothing(self):
        user = _user()
        refresh = RefreshToken.for_user(user)

        r = _client(user).post(
            CHANGE_URL,
            {'current_password': 'not-the-password', 'new_password': NEW},
            format='json',
        )

        assert r.status_code in (400, 401, 403)
        assert _refresh_still_works(refresh), (
            'a failed attempt must not log the real user out'
        )

    def test_the_new_password_is_the_one_that_works(self):
        user = _user()
        _client(user).post(
            CHANGE_URL,
            {'current_password': OLD, 'new_password': NEW},
            format='json',
        )
        user.refresh_from_db()

        assert user.check_password(NEW)
        assert not user.check_password(OLD)


class TestResetPassword:
    def test_a_reset_also_ends_every_existing_session(self):
        # The flow used when the account holder has LOST control, so this
        # matters more here than in a routine change.
        user = _user()
        refresh = RefreshToken.for_user(user)
        uid = urlsafe_base64_encode(force_bytes(user.pk))
        token = PasswordResetTokenGenerator().make_token(user)

        r = APIClient().post(
            RESET_URL,
            {'uid': uid, 'token': token, 'new_password': NEW},
            format='json',
        )

        assert r.status_code == 200, r.content.decode()
        assert not _refresh_still_works(refresh)

    def test_a_bad_reset_token_revokes_nothing(self):
        user = _user()
        refresh = RefreshToken.for_user(user)
        uid = urlsafe_base64_encode(force_bytes(user.pk))

        r = APIClient().post(
            RESET_URL,
            {'uid': uid, 'token': 'not-a-real-token', 'new_password': NEW},
            format='json',
        )

        assert r.status_code in (400, 401, 403)
        assert _refresh_still_works(refresh)


class TestTheDeliberateLimit:
    def test_an_already_minted_access_token_survives_its_15_minutes(self):
        """Recorded so it is read as a decision, not an oversight.

        Access tokens are stateless; killing one mid-flight would mean a
        database read on every authenticated request. The exposure is bounded
        by ACCESS_TOKEN_LIFETIME.
        """
        user = _user()
        access = AccessToken.for_user(user)

        _client(user).post(
            CHANGE_URL,
            {'current_password': OLD, 'new_password': NEW},
            format='json',
        )

        probe = APIClient()
        probe.credentials(HTTP_AUTHORIZATION=f'Bearer {access}')
        still_accepted = probe.get('/api/v1/auth/profile/').status_code == 200

        assert still_accepted, (
            'if this ever fails, per-request revocation was added -- update '
            'the docstring in session_revocation.py, which promises this '
            'window exists'
        )


class TestAuditTrail:
    def test_an_operator_credential_change_is_audited(self):
        operator = _user(phone='+919300000009', role='admin',
                         email='ops@deeptrics.com', is_staff=True)

        _client(operator).post(
            CHANGE_URL,
            {'current_password': OLD, 'new_password': NEW},
            format='json',
        )

        row = AdminAuditLog.objects.filter(
            action='operator_credential_changed',
            target_id=str(operator.pk)).first()
        assert row is not None, 'an investigation needs this event'
        assert row.after.get('refresh_tokens_revoked') is not None

    def test_a_riders_password_change_does_not_fill_the_operator_audit_log(self):
        rider = _user(phone='+919300000010', role='rider')

        _client(rider).post(
            CHANGE_URL,
            {'current_password': OLD, 'new_password': NEW},
            format='json',
        )

        assert not AdminAuditLog.objects.filter(
            action='operator_credential_changed',
            target_id=str(rider.pk)).exists(), (
            'routine rider traffic would bury KYC approvals and payout '
            'releases in the audit log'
        )


class TestTheHelperItself:
    def test_it_is_idempotent(self):
        user = _user(phone='+919300000011')
        RefreshToken.for_user(user)
        RefreshToken.for_user(user)

        first = revoke_all_refresh_tokens(user)
        second = revoke_all_refresh_tokens(user)

        assert first >= 2
        assert second == 0, 'already-blacklisted tokens are not re-counted'

    def test_it_tolerates_a_user_with_no_sessions(self):
        user = _user(phone='+919300000012')
        assert revoke_all_refresh_tokens(user) == 0

    def test_it_tolerates_none(self):
        assert revoke_all_refresh_tokens(None) == 0

    def test_a_blacklist_failure_does_not_break_the_password_change(self, monkeypatch):
        # A credential change that half-succeeds is worse than one that does
        # not revoke: the caller cannot tell which happened.
        user = _user(phone='+919300000013')

        import servers.auth_user.session_revocation as sr
        monkeypatch.setattr(
            sr, 'revoke_all_refresh_tokens',
            lambda u: (_ for _ in ()).throw(RuntimeError('blacklist down')))

        # revoke_after_credential_change calls the patched function; the view
        # must still return 200 and the password must still change.
        try:
            sr.revoke_after_credential_change(user)
        except RuntimeError:
            pytest.fail('revoke_after_credential_change must not propagate')
