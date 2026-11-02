"""Email sign-up requires an SMS-proved phone number (AUDIT 27.78, H4).

Before this, `/auth/email/register/` took the phone number on trust. The
number is the account's unique identity, so an attacker could register the
victim's number with the attacker's own email and password, and the victim
could then never sign up. Each test below fails on the code before the fix
(registration answered 201 without any proof) and passes after it.
"""

import time
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from servers.auth_user import phone_proof

pytestmark = pytest.mark.django_db

User = get_user_model()

OTP = '/api/v1/auth/otp/'
VERIFY = '/api/v1/auth/otp/verify/'
LOGIN = '/api/v1/auth/login/'
REGISTER = '/api/v1/auth/email/register/'

# A QA test phone from settings_ci (fixed code, no SMS) and an ordinary number.
TEST_PHONE, TEST_CODE = '+919999000001', '100001'
VICTIM = '+919870004321'
ATTACKER = '+919870004322'


def _form(phone, **extra):
    return {
        'full_name': 'Asha Rao', 'phone_number': phone,
        'email': extra.pop('email', 'asha.proof@example.test'),
        'password': 'Monsoon-Ferry-2026!', 'confirm_password': 'Monsoon-Ferry-2026!',
        'role': extra.pop('role', 'rider'), **extra,
    }


def _register(phone, **extra):
    return APIClient().post(REGISTER, _form(phone, **extra), format='json')


def _code(r):
    return r.json()['error']['code']


@pytest.fixture
def no_sms():
    """Real numbers send an SMS through Celery; capture the text instead."""
    sent = []
    with mock.patch('servers.auth_user.views.send_otp_via_sns.delay',
                    side_effect=lambda phone, ref: sent.append(phone) or 'task'):
        yield sent


class TestRegistrationRefusesWithoutAProof:
    def test_no_proof_is_refused_and_nothing_is_created(self):
        r = _register(VICTIM)
        assert r.status_code == 400
        assert _code(r) == 'AUTH_PHONE_PROOF_REQUIRED'
        assert r.json()['error']['details']['field'] == 'phone_proof'
        assert not User.objects.filter(phone_number=VICTIM).exists()

    def test_another_phones_proof_is_refused(self):
        r = _register(VICTIM, phone_proof=phone_proof.issue(ATTACKER, 'rider'))
        assert r.status_code == 400
        assert _code(r) == 'AUTH_PHONE_PROOF_INVALID'
        assert not User.objects.filter(phone_number=VICTIM).exists()

    def test_a_proof_for_another_role_is_refused(self):
        r = _register(VICTIM, role='driver', phone_proof=phone_proof.issue(VICTIM, 'rider'))
        assert _code(r) == 'AUTH_PHONE_PROOF_INVALID'

    def test_a_forged_proof_is_refused(self):
        r = _register(VICTIM, phone_proof='not-a-real-proof:forged:sig')
        assert _code(r) == 'AUTH_PHONE_PROOF_INVALID'

    def test_a_reused_proof_is_refused(self):
        proof = phone_proof.issue(VICTIM, 'rider')
        assert _register(VICTIM, phone_proof=proof).status_code == 201
        again = _register(VICTIM, phone_proof=proof, email='other.proof@example.test')
        assert again.status_code == 400
        assert _code(again) == 'AUTH_PHONE_PROOF_USED'

    def test_an_expired_proof_is_refused(self):
        proof = phone_proof.issue(VICTIM, 'rider')
        later = time.time() + phone_proof.ttl_seconds() + 5
        with mock.patch('django.core.signing.time.time', return_value=later):
            r = _register(VICTIM, phone_proof=proof)
        assert r.status_code == 400
        assert _code(r) == 'AUTH_PHONE_PROOF_EXPIRED'
        assert not User.objects.filter(phone_number=VICTIM).exists()

    def test_a_proof_for_another_number_does_not_burn_the_real_one(self):
        # Binding is checked before the nonce is spent.
        proof = phone_proof.issue(VICTIM, 'rider')
        assert _code(_register(ATTACKER, phone_proof=proof)) == 'AUTH_PHONE_PROOF_INVALID'
        assert _register(VICTIM, phone_proof=proof).status_code == 201


class TestTheVerifyStep:
    def test_happy_path_through_the_real_endpoints_with_a_qa_test_phone(self):
        client = APIClient()
        assert client.post(OTP, {'phone_number': TEST_PHONE, 'role': 'rider'},
                           format='json').status_code == 200
        v = client.post(VERIFY, {'phone_number': TEST_PHONE, 'otp': TEST_CODE}, format='json')
        assert v.status_code == 200, v.content.decode()
        body = v.json()['data']
        assert body['expires_in'] == phone_proof.ttl_seconds()
        # Verifying creates no account and hands out no session.
        assert not User.objects.filter(phone_number=TEST_PHONE).exists()
        assert 'token' not in body and 'refresh_token' not in body

        r = _register(TEST_PHONE, phone_proof=body['phone_proof'])
        assert r.status_code == 201, r.content.decode()
        assert User.objects.get(phone_number=TEST_PHONE).email == 'asha.proof@example.test'

    def test_the_code_is_spent_by_the_verify_step(self):
        client = APIClient()
        client.post(OTP, {'phone_number': TEST_PHONE, 'role': 'rider'}, format='json')
        assert client.post(VERIFY, {'phone_number': TEST_PHONE, 'otp': TEST_CODE},
                           format='json').status_code == 200
        # The same code cannot then also sign in, or prove the number twice.
        assert _code(client.post(LOGIN, {'phone_number': TEST_PHONE, 'otp': TEST_CODE},
                                 format='json')) == 'AUTH_OTP_EXPIRED'
        assert _code(client.post(VERIFY, {'phone_number': TEST_PHONE, 'otp': TEST_CODE},
                                 format='json')) == 'AUTH_OTP_EXPIRED'

    def test_the_proof_carries_the_role_the_code_was_requested_for(self):
        client = APIClient()
        client.post(OTP, {'phone_number': TEST_PHONE, 'role': 'driver'}, format='json')
        proof = client.post(VERIFY, {'phone_number': TEST_PHONE, 'otp': TEST_CODE},
                            format='json').json()['data']['phone_proof']
        assert _code(_register(TEST_PHONE, phone_proof=proof)) == 'AUTH_PHONE_PROOF_INVALID'


class TestTheReviewScenario:
    """The attacker types the VICTIM's number into the website sign-up."""

    def test_the_attacker_cannot_register_the_victims_number(self, no_sms):
        attacker = APIClient()
        # The code goes to the victim's handset, not to the attacker.
        assert attacker.post(OTP, {'phone_number': VICTIM, 'role': 'rider'},
                             format='json').status_code == 200
        assert no_sms == [VICTIM]

        # Guessing is bounded by the same per-code counter as sign-in.
        for guess in ('000000', '111111', '222222', '333333', '444444'):
            r = attacker.post(VERIFY, {'phone_number': VICTIM, 'otp': guess}, format='json')
            assert r.status_code == 400
        assert attacker.post(VERIFY, {'phone_number': VICTIM, 'otp': '555555'},
                             format='json').status_code == 429

        # No proof, or the attacker's own number's proof: both refused.
        assert _code(_register(VICTIM)) == 'AUTH_PHONE_PROOF_REQUIRED'
        own = phone_proof.issue(ATTACKER, 'rider')
        assert _code(_register(VICTIM, phone_proof=own)) == 'AUTH_PHONE_PROOF_INVALID'
        assert not User.objects.filter(phone_number=VICTIM).exists()

    def test_the_victim_can_still_sign_up_afterwards(self, no_sms):
        assert _code(_register(VICTIM)) == 'AUTH_PHONE_PROOF_REQUIRED'
        victim = APIClient()
        victim.post(OTP, {'phone_number': VICTIM, 'role': 'rider'}, format='json')
        from django.core.cache import cache
        code = cache.get(f'otp_role_{VICTIM}')['otp']
        r = victim.post(LOGIN, {'phone_number': VICTIM, 'otp': code}, format='json')
        assert r.status_code == 200
        assert User.objects.get(phone_number=VICTIM).email in (None, '')
