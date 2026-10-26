"""Close the TRUNCATE hole in the append-only guard.

Migration 0002 added BEFORE UPDATE and BEFORE DELETE triggers. Those are
ROW-level triggers, and PostgreSQL does not fire row-level triggers for
`TRUNCATE` — it is a table-level operation that removes every row without
touching one individually.

So the entire audit log could still be erased in a single statement, by the
same credentials the application already holds. `TRUNCATE` needs its own
STATEMENT-level trigger.

This is the kind of gap that only shows up when somebody asks "what else
removes rows?", which is why it is worth stating in the schema rather than
in a comment somewhere: the guard is now UPDATE, DELETE and TRUNCATE, and a
future reader can see all three.
"""

from django.db import migrations

CREATE_TRUNCATE_TRIGGER = """
DROP TRIGGER IF EXISTS saaradhigo_audit_no_truncate ON admin_audit_adminauditlog;
CREATE TRIGGER saaradhigo_audit_no_truncate
    BEFORE TRUNCATE ON admin_audit_adminauditlog
    FOR EACH STATEMENT EXECUTE FUNCTION saaradhigo_audit_append_only();
"""

DROP_TRUNCATE_TRIGGER = """
DROP TRIGGER IF EXISTS saaradhigo_audit_no_truncate ON admin_audit_adminauditlog;
"""


def add_trigger(apps, schema_editor):
    if schema_editor.connection.vendor != 'postgresql':
        return
    # params=None: the shared trigger function's body uses `%` as plpgsql's
    # RAISE placeholder. See 0002 for the full explanation.
    schema_editor.execute(CREATE_TRUNCATE_TRIGGER, params=None)


def remove_trigger(apps, schema_editor):
    if schema_editor.connection.vendor != 'postgresql':
        return
    schema_editor.execute(DROP_TRUNCATE_TRIGGER, params=None)


class Migration(migrations.Migration):

    dependencies = [
        ('admin_audit', '0003_hash_chain'),
    ]

    operations = [
        migrations.RunPython(add_trigger, remove_trigger),
    ]
