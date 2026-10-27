"""Start the hash chain honestly on a table that already had rows.

Migration 0003 added `content_hash` / `previous_hash` with `default=''` and,
correctly, did not rewrite the rows that already existed: the table is
append-only, and a row edited to look protected is the one thing the chain
must never contain. But it left `verify_audit_chain` with no way to tell
"written before the chain" from "hash stripped by an attacker", so on QA it
reported all 20 pre-chain rows as tampered, and would have done the same in
production for ever.

This migration appends ONE row -- a CHAIN_ANCHOR -- that records how many
unhashed rows precede it, the highest id among them, and a SHA-256 over
their canonical content in id order. Nothing existing is touched. The
verifier accepts unhashed rows only as one leading run vouched for by this
anchor, recomputes the digest, and fails on any disagreement. See
`hashchain.py`, "ROWS THAT PREDATE THE CHAIN".

On a database that never had pre-chain rows (a fresh install, CI) there is
nothing to vouch for and no row is written.

Not reversible by design: the row it writes cannot be deleted (migrations
0002 and 0004), and un-vouching for the pre-chain rows would only bring the
false alarm back.
"""

from django.db import migrations

ANCHOR_LABEL = 'migration admin_audit.0005_chain_anchor'


def add_anchor(apps, schema_editor):
    # Imported here rather than at module level so the migration loader
    # never pays for it; hashchain imports no models, so a historical model
    # is as good as the real one to it.
    from servers.admin_audit.hashchain import write_anchor

    AdminAuditLog = apps.get_model('admin_audit', 'AdminAuditLog')
    write_anchor(AdminAuditLog, label=ANCHOR_LABEL)


class Migration(migrations.Migration):

    dependencies = [
        ('admin_audit', '0004_no_truncate'),
    ]

    operations = [
        migrations.RunPython(add_anchor, migrations.RunPython.noop),
    ]
