"""A refused write to the object store must not read as "unexpected error".

Measured on QA: both a presigned PUT and a server-side PutObject to the
document bucket answer `403 SignatureDoesNotMatch` -- the credentials the
deployment holds do not match the bucket. Every document upload therefore
returned HTTP 500 and the single sentence "An unexpected error occurred",
which told the driver nothing, told support nothing, and gave on-call no
hint that the cause was configuration rather than code.

A storage outage is a 503 with a sentence that says the problem is ours.
"""

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from base.storage_health import (
    STORAGE_UNAVAILABLE_MESSAGE,
    save_with_storage_errors,
)


def _client_error(code='SignatureDoesNotMatch', operation='PutObject'):
    return ClientError({'Error': {'Code': code}}, operation)


class TestItTranslatesAStorageFailure:
    def test_a_refused_write_becomes_503(self):
        def save():
            raise _client_error()

        result, response = save_with_storage_errors(save, what='test')

        assert result is None
        assert response.status_code == 503
        body = response.data['error']
        assert body['code'] == 'STORAGE_UNAVAILABLE'
        assert body['message'] == STORAGE_UNAVAILABLE_MESSAGE

    def test_an_unreachable_endpoint_becomes_503(self):
        def save():
            raise EndpointConnectionError(endpoint_url='https://example.invalid')

        result, response = save_with_storage_errors(save, what='test')

        assert result is None
        assert response.status_code == 503

    def test_the_message_names_no_infrastructure(self):
        """A driver reading an error should not learn our storage topology."""
        def save():
            raise _client_error()

        _, response = save_with_storage_errors(save, what='test')

        shown = response.data['error']['message'].lower()
        for leak in ('bucket', 's3', 'signature', 'credential', 'aws', 'key'):
            assert leak not in shown, f'the message leaks {leak!r}'

    def test_the_reason_is_kept_for_operators(self):
        """The generic sentence is for the driver; `issue` is for us."""
        def save():
            raise _client_error(code='AccessDenied', operation='PutObject')

        _, response = save_with_storage_errors(save, what='test')

        issue = response.data['error']['details']['issue']
        assert 'PutObject' in issue and 'AccessDenied' in issue


class TestItDoesNotSwallowRealBugs:
    def test_an_ordinary_exception_still_propagates(self):
        """Only storage failures are translated. A TypeError in our own code
        must still be a 500 and a traceback, not a polite 503."""
        def save():
            raise TypeError('a real bug')

        with pytest.raises(TypeError):
            save_with_storage_errors(save, what='test')

    def test_a_successful_save_returns_its_result(self):
        result, response = save_with_storage_errors(lambda: 'saved', what='test')

        assert result == 'saved'
        assert response is None
