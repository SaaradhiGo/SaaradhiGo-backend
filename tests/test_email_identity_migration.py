"""`auth_user/0006_email_identity` has to fail on the data, not on the schema.

WHAT THIS MIGRATION DOES
------------------------
It lower-cases every stored address, narrows `email` from varchar(256) to
varchar(254), and adds a case-insensitive partial unique index.

TWO THINGS IN THE DATA CAN STOP IT
----------------------------------
1. Two accounts sharing an address -- the unique index cannot be created.
2. An address longer than 254 characters -- the narrowing is refused by the
   database with `value too long for type character varying(254)`, which
   names neither the column's purpose nor the row.

The first was already guarded. The second was not, and it is reachable: the
column is varchar(256) today, so a stored 255- or 256-character address is
legal right now and becomes illegal the moment this migration runs. A deploy
would have discovered it inside `preDeployCommand` as a raw database error.

Both are now detected before anything is written, and reported together.

WHAT IS ASSERTED, AND WHERE
---------------------------
The default (SQLite) run drives the pre-check directly: SQLite does not
enforce varchar widths, so an over-long value can be put in front of the
check there. The `postgres`-marked test does what only a real database can --
migrate backwards to 0005, write data that is legal at that width, and
migrate forwards again -- which is also the only way to prove the reverse
path runs at all.
"""

import importlib

import pytest
from django.apps import apps
from django.contrib.auth import get_user_model
from django.db import connection

migration = importlib.import_module(
    'servers.auth_user.migrations.0006_email_identity')

EMAIL_MAX_LENGTH = migration.EMAIL_MAX_LENGTH

pytestmark = pytest.mark.django_db

User = get_user_model()

TABLE = User._meta.db_table


def _user(phone, email=None):
    return User.objects.create_user(
        phone_number=phone, username=phone, role='rider', email=email)


def _address(total_length, domain='@example.com'):
    """An address of exactly `total_length` characters."""
    local = 'a' * (total_length - len(domain))
    assert local, 'domain alone is longer than the requested length'
    return local + domain


def _run_precheck():
    """Call the migration's pre-check against the live app registry."""
    migration.normalise_and_check_emails(apps, connection.schema_editor())



@pytest.fixture
def before_the_constraint(db):
    """The schema as it stands BEFORE this migration: no unique index.

    Duplicate addresses cannot be created once the index exists -- which is
    exactly why the pre-check matters. The data it guards against can only
    be in the database because it predates the constraint, so reproducing it
    means reproducing that state.
    """
    with connection.cursor() as cursor:
        cursor.execute('DROP INDEX IF EXISTS uniq_user_email_ci')
    yield

# ---------------------------------------------------------------------------
# The length check -- the gap this change closes
# ---------------------------------------------------------------------------

class TestOverLongAddresses:
    def test_an_address_over_the_limit_stops_the_migration(self):
        _user('+919991110001', email=_address(EMAIL_MAX_LENGTH + 1))

        with pytest.raises(RuntimeError) as excinfo:
            _run_precheck()

        assert 'longer than 254 characters' in str(excinfo.value)

    def test_an_address_exactly_at_the_limit_is_fine(self):
        # 254 fits. Off-by-one here would reject valid data and block a
        # deploy for no reason.
        _user('+919991110002', email=_address(EMAIL_MAX_LENGTH))

        _run_precheck()  # does not raise

    def test_the_report_names_the_count_the_longest_and_the_ids(self):
        a = _user('+919991110003', email=_address(260))
        b = _user('+919991110004', email=_address(300))

        with pytest.raises(RuntimeError) as excinfo:
            _run_precheck()

        message = str(excinfo.value)
        assert '2 email address(es) are longer' in message
        assert 'longest: 300' in message
        assert str(a.pk) in message and str(b.pk) in message

    def test_the_report_does_not_contain_the_address(self):
        # A deployment log must not become a list of customer emails.
        _user('+919991110005',
              email='distinctive-local-part' + 'a' * 240 + '@example.com')

        with pytest.raises(RuntimeError) as excinfo:
            _run_precheck()

        assert 'distinctive-local-part' not in str(excinfo.value)
        assert '@example.com' not in str(excinfo.value)

    def test_whitespace_padding_is_trimmed_rather_than_rejected(self):
        # The stored value after this migration is the NORMALISED one, so an
        # address that only exceeds the limit because of surrounding spaces
        # is fixed here, not refused.
        padded = '   ' + _address(EMAIL_MAX_LENGTH) + '   '
        user = _user('+919991110006', email=padded)
        assert len(padded) > EMAIL_MAX_LENGTH

        _run_precheck()

        user.refresh_from_db()
        assert len(user.email) == EMAIL_MAX_LENGTH
        assert user.email == user.email.strip().lower()


# ---------------------------------------------------------------------------
# The duplicate check -- preserved behaviour
# ---------------------------------------------------------------------------

class TestDuplicatesStillCaught:
    def test_two_accounts_sharing_an_address_stop_the_migration(
            self, before_the_constraint):
        _user('+919992220001', email='Shared@Example.com')
        _user('+919992220002', email='shared@example.com')

        with pytest.raises(RuntimeError) as excinfo:
            _run_precheck()

        message = str(excinfo.value)
        assert 'shared by 2' in message
        assert 'shared@example.com' not in message

    def test_the_duplicate_report_names_the_account_ids(
            self, before_the_constraint):
        a = _user('+919992220003', email='dup@example.com')
        b = _user('+919992220004', email='DUP@example.com')

        with pytest.raises(RuntimeError) as excinfo:
            _run_precheck()

        assert str(a.pk) in str(excinfo.value)
        assert str(b.pk) in str(excinfo.value)

    def test_blank_and_null_addresses_are_not_duplicates_of_each_other(self):
        # Thousands of phone-only accounts have no address. The partial index
        # excludes them, and so must this check.
        _user('+919992220005', email=None)
        _user('+919992220006', email='')
        _user('+919992220007', email=None)

        _run_precheck()  # does not raise


class TestBothProblemsAtOnce:
    def test_one_run_reports_length_and_duplicates_together(
            self, before_the_constraint):
        # Otherwise the operator fixes one, re-runs the deploy, and discovers
        # the other.
        _user('+919993330001', email=_address(270))
        _user('+919993330002', email='both@example.com')
        _user('+919993330003', email='BOTH@example.com')

        with pytest.raises(RuntimeError) as excinfo:
            _run_precheck()

        message = str(excinfo.value)
        assert 'longer than 254' in message
        assert 'shared by 2' in message


class TestNothingIsWrittenWhenItRefuses:
    def test_normalisation_does_not_happen_on_a_failed_check(self):
        # Checks first, writes after: a refused migration must leave the data
        # exactly as it was, so the operator's own queries still match what
        # they saw before.
        keeper = _user('+919994440001', email='MixedCase@Example.com')
        _user('+919994440002', email=_address(300))

        with pytest.raises(RuntimeError):
            _run_precheck()

        keeper.refresh_from_db()
        assert keeper.email == 'MixedCase@Example.com'


class TestTheHappyPath:
    def test_addresses_are_lower_cased_and_trimmed(self):
        user = _user('+919995550001', email='  Rider@Example.COM ')

        _run_precheck()

        user.refresh_from_db()
        assert user.email == 'rider@example.com'

    def test_an_already_normalised_address_is_left_alone(self):
        user = _user('+919995550002', email='clean@example.com')

        _run_precheck()

        user.refresh_from_db()
        assert user.email == 'clean@example.com'


# ---------------------------------------------------------------------------
# PostgreSQL: the real forwards/backwards round trip
# ---------------------------------------------------------------------------

def _migrate(target):
    """Run auth_user to `target` in whichever direction that requires."""
    from django.db.migrations.executor import MigrationExecutor

    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate([('auth_user', target)])


def _latest_auth_user_migration():
    """The newest auth_user migration, resolved rather than hardcoded.

    These tests rewind the schema and must put it back. They used to put it
    back to a literal '0006_email_identity', which was the newest migration
    when they were written. The moment a later one existed -- 0007 added
    `is_break_glass` -- every test here left the database one migration
    short of the live model, and the next query through the ORM failed with
    `column "is_break_glass" does not exist`. The tests still "passed"
    their own assertions; what broke was everything after them.

    Resolving the target means the next field added to CustomUser does not
    reintroduce this.
    """
    from django.db.migrations.loader import MigrationLoader

    loader = MigrationLoader(connection)
    names = sorted(
        name for app, name in loader.graph.nodes if app == 'auth_user'
    )
    return names[-1]


def _email_column_width():
    with connection.cursor() as cursor:
        cursor.execute(
            'SELECT character_maximum_length FROM information_schema.columns '
            'WHERE table_name = %s AND column_name = %s',
            [TABLE, 'email'],
        )
        return cursor.fetchone()[0]


def _unique_index_exists():
    with connection.cursor() as cursor:
        cursor.execute(
            'SELECT COUNT(*) FROM pg_indexes '
            'WHERE tablename = %s AND indexname = %s',
            [TABLE, 'uniq_user_email_ci'],
        )
        return cursor.fetchone()[0] == 1


def _set_email_raw(pk, value):
    """Write past the ORM, which knows nothing of the 0005 column width."""
    with connection.cursor() as cursor:
        cursor.execute(
            f'UPDATE {TABLE} SET email = %s WHERE id = %s', [value, pk])


@pytest.mark.postgres
@pytest.mark.django_db(transaction=True)
class TestOnPostgres:
    """Forwards, backwards, and forwards again, against a real varchar.

    Run with: pytest -m postgres (DB_HOST etc. pointing at a throwaway
    PostgreSQL). Each test leaves the database back at the NEWEST auth_user
    migration -- resolved, not hardcoded; see `_latest_auth_user_migration`.
    """

    def test_the_column_narrows_forward_and_widens_back(self):
        assert _email_column_width() == EMAIL_MAX_LENGTH
        assert _unique_index_exists()

        try:
            _migrate('0005_operator_authority')

            assert _email_column_width() == 256, (
                'the pre-migration width -- this is the window in which an '
                'over-long address can legally be stored'
            )
            assert not _unique_index_exists()
        finally:
            _migrate(_latest_auth_user_migration())

        assert _email_column_width() == EMAIL_MAX_LENGTH
        assert _unique_index_exists()

    def test_the_reverse_runs_rather_than_raising_irreversible(self):
        # A RunPython with no reverse makes the whole migration
        # irreversible, which would leave a failed deploy with no way back.
        try:
            _migrate('0005_operator_authority')
        finally:
            _migrate(_latest_auth_user_migration())

    def test_an_address_that_is_legal_at_0005_is_caught_going_forward(self):
        # The real-world case: 255 and 256 characters fit the current column
        # and are refused by the new one. Without the pre-check this is a raw
        # `value too long for type character varying(254)` inside the
        # deployment's preDeployCommand.
        user = _user('+919996660001', email='short@example.com')

        try:
            _migrate('0005_operator_authority')
            _set_email_raw(user.pk, _address(256))

            with pytest.raises(RuntimeError) as excinfo:
                _migrate(_latest_auth_user_migration())

            assert 'longer than 254 characters' in str(excinfo.value)
            assert _email_column_width() == 256, 'the failure rolled back'

            # Fix the data the way the error message says to, then proceed.
            _set_email_raw(user.pk, 'short@example.com')
            _migrate(_latest_auth_user_migration())

            assert _email_column_width() == EMAIL_MAX_LENGTH
            user.refresh_from_db()
            assert user.email == 'short@example.com'
        finally:
            _migrate(_latest_auth_user_migration())

    def test_the_unique_index_is_case_insensitive_and_partial(self):
        # What the migration exists to create, asserted against the database
        # rather than against the model definition.
        from django.db import IntegrityError

        _user('+919997770001', email='Case@Example.com')

        with pytest.raises(IntegrityError):
            _user('+919997770002', email='case@example.com')

    def test_many_accounts_without_an_address_coexist(self):
        # The partial condition: NULL and '' are excluded, so phone-only
        # accounts do not collide with each other.
        for n in range(5):
            _user(f'+91999888{n:04d}', email=None)
        for n in range(5):
            _user(f'+91999777{n:04d}', email='')

        assert User.objects.filter(email__isnull=True).count() >= 5
