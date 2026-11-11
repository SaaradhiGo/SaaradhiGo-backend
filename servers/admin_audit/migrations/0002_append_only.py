"""Make the admin audit log append-only in the DATABASE.

WHY THIS IS A MIGRATION AND NOT ONLY PYTHON
-------------------------------------------
`AdminAuditLog` is the only record of who approved a payout, who passed a
driver's KYC, and who changed a price. Model-level guards protect it from
our own code. They do nothing about a psql session, a Django shell, a
data-migration written in a hurry, or an ORM path that bypasses the
manager.

The table is the thing that has to refuse. So PostgreSQL refuses: a trigger
raises on UPDATE and on DELETE, for every caller, including superusers
going through the ORM.

WHY A TRIGGER AND NOT REVOKED GRANTS
------------------------------------
Revoking UPDATE/DELETE from the application role is also worth doing, but it
is deployment configuration rather than schema: it lives in whoever
provisions the database, it is invisible from the repository, and on
Railway the application connects as the owner. A trigger travels with the
migration, applies to every role including the owner, and is visible to
anyone reading this file. Do both if you have a DBA; this is the half that
cannot be forgotten.

SQLITE
------
CI and local development run on SQLite, which has no equivalent we can
express portably here. The model and queryset raise `AuditLogIsAppendOnly`
there, and the test suite asserts that behaviour on both backends. The
PostgreSQL-only test is skipped rather than silently passing.
"""

from django.db import migrations

APPEND_ONLY_FUNCTION = """
CREATE OR REPLACE FUNCTION saaradhigo_audit_append_only()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION
        'admin_audit_adminauditlog is append-only: % is not permitted. '
        'Record a correction as a new row.', TG_OP
        USING ERRCODE = 'restrict_violation';
END;
$$ LANGUAGE plpgsql;
"""

CREATE_TRIGGERS = """
DROP TRIGGER IF EXISTS saaradhigo_audit_no_update ON admin_audit_adminauditlog;
CREATE TRIGGER saaradhigo_audit_no_update
    BEFORE UPDATE ON admin_audit_adminauditlog
    FOR EACH ROW EXECUTE FUNCTION saaradhigo_audit_append_only();

DROP TRIGGER IF EXISTS saaradhigo_audit_no_delete ON admin_audit_adminauditlog;
CREATE TRIGGER saaradhigo_audit_no_delete
    BEFORE DELETE ON admin_audit_adminauditlog
    FOR EACH ROW EXECUTE FUNCTION saaradhigo_audit_append_only();
"""

DROP_TRIGGERS = """
DROP TRIGGER IF EXISTS saaradhigo_audit_no_update ON admin_audit_adminauditlog;
DROP TRIGGER IF EXISTS saaradhigo_audit_no_delete ON admin_audit_adminauditlog;
DROP FUNCTION IF EXISTS saaradhigo_audit_append_only();
"""


def add_triggers(apps, schema_editor):
    # PostgreSQL only. On SQLite the model-level guard is the whole story,
    # and a migration that pretended otherwise would be a lie in the
    # schema history.
    if schema_editor.connection.vendor != 'postgresql':
        return
    # `params=None`, not the default `()`.
    #
    # The function body contains `%` as plpgsql's own format placeholder in
    # `RAISE EXCEPTION ... %`. With a params tuple, psycopg mogrifies the
    # statement first and reads those as SQL parameter markers, which fails
    # with `IndexError: tuple index out of range` before the trigger is ever
    # created. `params=None` sends the SQL through untouched.
    schema_editor.execute(APPEND_ONLY_FUNCTION, params=None)
    schema_editor.execute(CREATE_TRIGGERS, params=None)


def remove_triggers(apps, schema_editor):
    if schema_editor.connection.vendor != 'postgresql':
        return
    schema_editor.execute(DROP_TRIGGERS, params=None)


class Migration(migrations.Migration):

    dependencies = [
        ('admin_audit', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(
            add_triggers,
            # Reversible on purpose. A migration that cannot be rolled back
            # is a migration nobody dares apply, and the guard being
            # removable by an explicit, reviewed `migrate` is different
            # from it being removable by an UPDATE statement.
            remove_triggers,
        ),
    ]
