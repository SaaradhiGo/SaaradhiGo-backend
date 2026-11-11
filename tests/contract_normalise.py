"""Make a regenerated contract file identical to the committed one unless
the API actually changed.

WHY
---
`test_auth_contract_capture.py` and `test_ride_contract_capture.py` write the
server's real response bodies to `contracts/*.json`, and that file is
committed on purpose: a diff on it is the review signal that an API contract
moved. But two of the values in every response move on every run without
the contract moving at all -- timestamps, and the JWTs that encode them.
So every local `pytest` rewrote both files, and the working tree ended up
with 82 changed lines that said nothing. A signal that fires every time is
not a signal; and uncommitted churn in a shared checkout is how edits get
discarded or committed by accident.

This replaces exactly those two kinds of value with FIXED, VALID stand-ins
before the file is written:

  * any ISO-8601 datetime string  -> one fixed instant;
  * any JWT                       -> one fixed, well-formed JWT whose claims
                                     name the token type, with an expiry far
                                     in the future.

Fixed and valid matters: the two Flutter apps feed these files through
their REAL parsing code, so a placeholder like `<timestamp>` would fail
`DateTime.parse` and a JWT with `exp: 0` would read as expired. Nothing
else is touched, so a renamed field, a new status code or a changed shape
still changes the file and still fails the apps' tests.

A test in `test_contract_normalise.py` asserts the committed files are
already in this form, so a file written without normalising cannot land.
"""

import base64
import json
import re

ISO_DATETIME = re.compile(
    r'^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})?$'
)
JWT = re.compile(r'^eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$')

#: One instant, in the server's own timezone offset, microsecond precision,
#: the exact shape DRF renders.
FIXED_TIMESTAMP = '2026-01-01T00:00:00.000000+05:30'

#: Far enough ahead that no app test treats the token as expired.
_FIXED_EXP = 4102444800  # 2100-01-01T00:00:00Z


def _b64(obj) -> str:
    raw = json.dumps(obj, separators=(',', ':')).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode()


def fixed_jwt(token_type: str) -> str:
    """A well-formed, unsigned-in-practice JWT naming its type.

    Same header and claim names SimpleJWT emits, so an app that decodes the
    payload to read `exp` or `user_id` still finds them.
    """
    header = {'alg': 'HS256', 'typ': 'JWT'}
    payload = {
        'token_type': token_type, 'exp': _FIXED_EXP, 'iat': 0,
        'jti': '0' * 32, 'user_id': '1', 'iss': 'saaradhigo',
    }
    return f'{_b64(header)}.{_b64(payload)}.{"0" * 43}'


FIXED_ACCESS_JWT = fixed_jwt('access')
FIXED_REFRESH_JWT = fixed_jwt('refresh')

_REFRESH_KEYS = {'refresh', 'refresh_token'}


def normalise(value, key=None):
    """Return `value` with every timestamp and JWT replaced, recursively.

    `key` is the name the value sits under, used only to decide whether a
    JWT is a refresh token. Idempotent: normalising twice changes nothing.
    """
    if isinstance(value, dict):
        return {k: normalise(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [normalise(v, key) for v in value]
    if isinstance(value, str):
        if JWT.match(value):
            return FIXED_REFRESH_JWT if key in _REFRESH_KEYS else FIXED_ACCESS_JWT
        if ISO_DATETIME.match(value):
            return FIXED_TIMESTAMP
    return value
