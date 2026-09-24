"""A deploy verifier must be able to tell which revision is serving.

This exists because of a specific, verified operational failure. A deploy was
checked by polling /healthz until it returned 200, then running a behaviour check
against the result. The check failed and the reported defect was wrong: the
deployment was still QUEUED and the 200 had been answered by the OLD container.

/healthz already carried a `version` field, which made the gap easy to miss. But it
read DJANGO_RELEASE -- a variable set by hand. On Railway that is a static service
variable, so it reported an identical string across every deploy and could not
distinguish one revision from another. It looked like revision visibility while
providing none.

The property under test is narrow and important: the endpoint must report the
revision from a source that changes on its own, must say WHICH source answered,
and must say 'unknown' rather than something plausible when it cannot tell.
"""

import json

import pytest

from base import health


@pytest.fixture(autouse=True)
def _clear_revision_env(monkeypatch):
    """Start every test with no revision in the environment.

    Otherwise a CI runner that happens to set GITHUB_SHA would make these pass
    or fail for reasons unrelated to the code.
    """
    for name in health._REVISION_SOURCES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(health, '_BUILD_STAMP', None)


def _get(client, path):
    resp = client.get(path)
    return resp, json.loads(resp.content)


# ---------------------------------------------------------------------------
# /version
# ---------------------------------------------------------------------------

def test_version_reports_the_platform_injected_commit(client, monkeypatch):
    """Railway injects this per build. It is the only value that self-updates."""
    monkeypatch.setenv('RAILWAY_GIT_COMMIT_SHA', 'a' * 40)

    resp, body = _get(client, '/version')

    assert resp.status_code == 200
    assert body['revision'] == 'a' * 40
    assert body['revision_source'] == 'RAILWAY_GIT_COMMIT_SHA'
    assert body['revision_short'] == 'aaaaaaa'


def test_a_platform_value_wins_over_a_hand_set_one(monkeypatch, client):
    """The precedence that matters.

    DJANGO_RELEASE is correct only if somebody remembered to update it. When both
    are present, the one the platform maintains must win, or a stale hand-set
    variable masks the real revision -- which is exactly what happened.
    """
    monkeypatch.setenv('DJANGO_RELEASE', 'v1.2.3-stale')
    monkeypatch.setenv('RAILWAY_GIT_COMMIT_SHA', 'b' * 40)

    _, body = _get(client, '/version')

    assert body['revision'] == 'b' * 40
    assert body['revision_source'] == 'RAILWAY_GIT_COMMIT_SHA'


def test_a_hand_set_release_is_still_honoured_when_it_is_all_there_is(
    monkeypatch, client,
):
    """Negative control: the fallback must keep working.

    Not every host injects a commit, and removing the fallback would leave those
    deployments with no revision at all.
    """
    monkeypatch.setenv('DJANGO_RELEASE', 'v1.2.3')

    _, body = _get(client, '/version')

    assert body['revision'] == 'v1.2.3'
    assert body['revision_source'] == 'DJANGO_RELEASE'


def test_an_unknown_revision_says_unknown(client):
    """It must never invent a plausible answer.

    A wrong revision is worse than an absent one: it is what lets a verifier
    confirm a deploy that never happened.
    """
    _, body = _get(client, '/version')

    assert body['revision'] == 'unknown'
    assert body['revision_source'] == 'none'
    assert body['revision_short'] == 'unknown'


def test_a_build_stamp_beats_a_hand_set_variable(monkeypatch, client, tmp_path):
    """A file written by the build is maintained by the build."""
    stamp = tmp_path / '.build-revision'
    stamp.write_text('c' * 40, encoding='utf-8')
    monkeypatch.setattr(health, '_BUILD_STAMP', str(stamp))

    _, body = _get(client, '/version')

    assert body['revision'] == 'c' * 40
    assert body['revision_source'] == 'build-stamp'


def test_version_needs_no_authentication(client):
    """A deploy script and an uptime monitor must not need credentials."""
    resp = client.get('/version')

    assert resp.status_code == 200


def test_version_does_no_database_work(client, monkeypatch):
    """It must be pollable every second during a deploy without adding load.

    A verifier hammering an endpoint that opens a connection would degrade the
    thing it is trying to observe -- and would report 'not ready' for a database
    problem rather than a revision mismatch.
    """
    from django.db import connection

    def _explode(*a, **k):
        raise AssertionError('/version touched the database')

    monkeypatch.setattr(connection, 'cursor', _explode)

    resp = client.get('/version')

    assert resp.status_code == 200


def test_version_exposes_nothing_sensitive(client, monkeypatch):
    """Whitelist, not blacklist: assert the exact key set."""
    monkeypatch.setenv('RAILWAY_GIT_COMMIT_SHA', 'd' * 40)

    _, body = _get(client, '/version')

    assert set(body) == {
        'service', 'environment', 'revision', 'revision_source',
        'revision_short', 'build_time',
    }, f'unexpected keys in the version payload: {sorted(body)}'


# ---------------------------------------------------------------------------
# /healthz keeps its contract and gains the revision
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_healthz_still_reports_health(client):
    resp, body = _get(client, '/healthz')

    assert resp.status_code == 200
    assert body['status'] == 'ok'
    assert body['db'] == 'ok'
    assert body['cache'] == 'ok'


@pytest.mark.django_db
def test_healthz_carries_the_revision_too(client, monkeypatch):
    """So one call can answer both questions."""
    monkeypatch.setenv('RAILWAY_GIT_COMMIT_SHA', 'e' * 40)

    _, body = _get(client, '/healthz')

    assert body['revision'] == 'e' * 40
    assert body['revision_source'] == 'RAILWAY_GIT_COMMIT_SHA'


@pytest.mark.django_db
def test_the_legacy_version_field_survives(client, monkeypatch):
    """Existing monitors and QA scripts read `version`. It must not vanish."""
    monkeypatch.setenv('RAILWAY_GIT_COMMIT_SHA', 'f' * 40)

    _, body = _get(client, '/healthz')

    assert 'version' in body
    assert body['version'] == 'f' * 40, (
        'the legacy field should now report the resolved revision rather than '
        'only DJANGO_RELEASE'
    )
