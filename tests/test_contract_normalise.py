"""The committed contract files must be stable across runs.

See `tests/contract_normalise.py` for why. The last test here is the one
that matters: it fails if either committed file contains a live timestamp
or JWT, i.e. if somebody wrote it without normalising.
"""

import json
import pathlib

from tests.contract_normalise import (
    FIXED_ACCESS_JWT, FIXED_REFRESH_JWT, FIXED_TIMESTAMP, normalise,
)

CONTRACTS = pathlib.Path(__file__).resolve().parent.parent / 'contracts'

LIVE_ACCESS = ('eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.'
               'eyJ0b2tlbl90eXBlIjoiYWNjZXNzIiwiZXhwIjoxNzkzMTMxNDA5fQ.'
               'mk7fzDZ66g6alUmju1OyXoUivLxZgDBR4jFddq_GMgU')


def test_timestamps_and_tokens_become_fixed_values():
    body = {
        'data': {
            'token': LIVE_ACCESS,
            'refresh_token': LIVE_ACCESS,
            'user': {'created_at': '2026-10-28T01:18:28.829710+05:30',
                     'username': 'contract-rider', 'id': 1},
            'history': [{'completed_at': '2026-10-27T02:29:02Z'}],
        },
        'meta': {'timestamp': '2026-10-28T01:18:29.417550+05:30'},
    }

    out = normalise(body)

    assert out['data']['token'] == FIXED_ACCESS_JWT
    assert out['data']['refresh_token'] == FIXED_REFRESH_JWT
    assert out['data']['user']['created_at'] == FIXED_TIMESTAMP
    assert out['data']['history'][0]['completed_at'] == FIXED_TIMESTAMP
    assert out['meta']['timestamp'] == FIXED_TIMESTAMP
    # Everything that is not a timestamp or a JWT is untouched.
    assert out['data']['user']['username'] == 'contract-rider'
    assert out['data']['user']['id'] == 1


def test_plain_strings_numbers_and_nulls_are_untouched():
    body = {'message': 'Logged out', 'expires_in': 600, 'avatar': None,
            'date_only': '2026-10-27', 'code': 'AUTH_MFA_REQUIRED',
            'amount_minor': 12345}

    assert normalise(body) == body


def test_normalising_is_idempotent():
    once = normalise({'t': LIVE_ACCESS, 'at': '2026-10-27T02:29:02.425305+05:30'})

    assert normalise(once) == once


def test_the_fixed_tokens_are_well_formed_and_name_their_type():
    import base64
    for token, expected in ((FIXED_ACCESS_JWT, 'access'),
                            (FIXED_REFRESH_JWT, 'refresh')):
        header, payload, _sig = token.split('.')
        claims = json.loads(base64.urlsafe_b64decode(payload + '=='))
        assert claims['token_type'] == expected
        assert claims['exp'] > 4_000_000_000  # far future: never "expired"


def test_the_committed_contract_files_contain_no_live_values():
    for name in ('auth_contracts.json', 'ride_contracts.json'):
        path = CONTRACTS / name
        if not path.exists():
            continue
        data = json.loads(path.read_text(encoding='utf-8'))
        assert normalise(data) == data, (
            f'{name} contains a live timestamp or JWT; regenerate it with '
            f'the capture test so the committed file is stable.'
        )
