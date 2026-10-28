"""The actor becomes a snapshot: drop the foreign key, add email and role.

WHY
---
`actor` was a FK with `on_delete=SET_NULL`. Deleting a staff account would
therefore UPDATE every audit row they ever wrote -- and on PostgreSQL the
append-only trigger (0002) refuses that UPDATE, so the delete raised from
inside the cascade. Even where it did not raise, blanking the author of an
audit row on request is the opposite of what an audit log is for.

The owner's ruling (2026-10-27): store the actor as a snapshot -- id with no
FK, email and role at write time -- exempt nothing from the trigger, never
delete staff, and anonymise riders and drivers who ask to be erased instead
of deleting them. This migration is the schema half; `servers.auth_user.
erasure` is the behaviour half.

WHAT THIS MIGRATION DOES, AND WHAT IT MUST NOT
----------------------------------------------
Only DDL: drop one constraint, add two columns. It issues no UPDATE and
touches no row, because the table is append-only and the trigger would
refuse anyway. The existing `actor_id` column keeps its name, its type
(bigint, matching the user table's BigAutoField) and both of its indexes;
the two new columns are added empty, so every existing row's hash still
verifies -- see `hashchain.py`, "COLUMNS ADDED AFTER THE CHAIN STARTED", and
`tests/test_audit_actor_snapshot.py`, which applies this migration to a
PostgreSQL table that already has hashed rows and runs the verifier.

WHY THE DATABASE AND THE STATE ARE CHANGED SEPARATELY
-----------------------------------------------------
The field is renamed from `actor` to `actor_id` in Python, but the COLUMN
was `actor_id` all along (a FK's column is `<name>_id`). Spelling that as
`RenameField` would be wrong twice: Django's state tracking does not carry
a rename into `Meta.indexes`, so `makemigrations --check` keeps proposing
to drop and recreate `admin_audit_actor_idx`; and on SQLite the table
rebuild copies the old model's index list verbatim and then cannot find a
field called `actor` on the new one. A `RemoveField`/`AddField` pair would
drop the column and every actor id in it.

So the database side is ONE `AlterField` -- FK to plain BigIntegerField on
the same column, which is exactly "drop the constraint" -- and the state
side records the field under its new name with the index pointing at it.
The physical index already exists (0001 created it on this column) and is
not touched.

Reversible: the reverse re-adds the FK constraint and drops the two
columns. Anonymisation keeps accounts, so every stored actor id still
resolves and the constraint can be rebuilt.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('admin_audit', '0005_chain_anchor'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                # Drops the FK constraint. Same column, same type, so
                # nothing else happens to the table.
                migrations.AlterField(
                    model_name='adminauditlog',
                    name='actor',
                    field=models.BigIntegerField(
                        blank=True, null=True, db_index=True,
                        db_column='actor_id',
                    ),
                ),
            ],
            state_operations=[
                migrations.RemoveIndex(
                    model_name='adminauditlog',
                    name='admin_audit_actor_idx',
                ),
                migrations.RemoveField(
                    model_name='adminauditlog',
                    name='actor',
                ),
                migrations.AddField(
                    model_name='adminauditlog',
                    name='actor_id',
                    field=models.BigIntegerField(blank=True, null=True,
                                                 db_index=True),
                ),
                migrations.AddIndex(
                    model_name='adminauditlog',
                    index=models.Index(fields=['actor_id', '-created_at'],
                                       name='admin_audit_actor_idx'),
                ),
            ],
        ),
        migrations.AlterField(
            model_name='adminauditlog',
            name='actor_label',
            field=models.CharField(
                blank=True, max_length=255,
                help_text='Human-readable snapshot of the actor at action '
                          'time (survives later deactivation/rename of the '
                          'user).',
            ),
        ),
        migrations.AddField(
            model_name='adminauditlog',
            name='actor_email',
            field=models.CharField(
                blank=True, default='', max_length=254,
                help_text="The actor's email at action time.",
            ),
        ),
        migrations.AddField(
            model_name='adminauditlog',
            name='actor_role',
            field=models.CharField(
                blank=True, default='', max_length=64,
                help_text="The actor's role, and ops_role for an operator "
                          "(`admin/finance`), at action time.",
            ),
        ),
    ]
