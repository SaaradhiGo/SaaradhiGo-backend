"""Tamper evidence for the audit log.

WHAT THIS IS FOR, AND WHAT IT IS NOT
------------------------------------
The append-only trigger (migration 0002) stops mistakes and stops ordinary
code. It does not stop someone determined: the application connects to the
database as the OWNER, and an owner can `ALTER TABLE … DISABLE TRIGGER`,
change rows, and switch it back on. Nothing inside the database can prevent
that while we hold owner credentials.

The real fix is a separate, less-privileged application role, and that is
tracked as a production blocker in docs/admin/AUDIT.md §14.

Until then: make tampering DETECTABLE. Each row stores

    content_hash  = SHA-256( previous_hash + canonical(this row) )

so every row commits to the entire history before it. Edit a row, remove a
row, or reorder two, and every hash after that point stops matching. The
chain cannot stop someone with owner rights from rewriting the table — but
to rewrite it *undetectably* they must recompute every subsequent hash,
which is a deliberate act that cannot be mistaken for an accident.

WHY THE CANONICAL FORM IS EXPLICIT
----------------------------------
The hash is only as good as the agreement on what was hashed. `str(dict)`
or `json.dumps` with default settings depend on insertion order and on
Python's float repr; either would make the chain fail for rows nobody
touched. So: sorted keys, no whitespace, explicit ISO-8601 for timestamps,
and `None` normalised to the empty string. Fields are listed one by one
rather than iterated from `_meta`, because adding a column later must not
silently change the hash of every historical row.
"""

import hashlib
import json

#: What the first row in the chain links to.
GENESIS = '0' * 64

#: The fields that are covered by the hash, in a fixed order.
#:
#: Adding a field here changes every future hash and invalidates the chain
#: for rows written before the change, so a new field requires a documented
#: chain restart rather than a quiet edit of this tuple.
HASHED_FIELDS = (
    'actor_id',
    'actor_label',
    'action',
    'target_type',
    'target_id',
    'before',
    'after',
    'reason',
    'ip_address',
    'user_agent',
    'created_at',
)


def _normalise(value):
    """A stable representation, independent of Python's repr choices."""
    if value is None:
        return ''
    if hasattr(value, 'isoformat'):
        return value.isoformat()
    if isinstance(value, (dict, list)):
        # sort_keys so two equal dicts hash equally regardless of the order
        # they happened to be built in; separators so whitespace cannot
        # change the digest.
        return json.dumps(value, sort_keys=True, separators=(',', ':'),
                          default=str)
    return str(value)


def canonical_content(entry):
    """The exact bytes covered by this row's hash."""
    parts = {field: _normalise(getattr(entry, field, None))
             for field in HASHED_FIELDS}
    return json.dumps(parts, sort_keys=True, separators=(',', ':'))


def compute_hash(previous_hash, entry):
    """`SHA-256(previous_hash + canonical(entry))`, as hex."""
    digest = hashlib.sha256()
    digest.update((previous_hash or GENESIS).encode('utf-8'))
    digest.update(canonical_content(entry).encode('utf-8'))
    return digest.hexdigest()


def verify_chain(queryset, *, limit=None):
    """Walk the log oldest-first and report the first break.

    Returns `(checked, problems)` where `problems` is a list of dicts
    describing each row that does not match. An empty list means every row
    hashes to what it claims and links to the row before it.

    Deliberately reports EVERY break rather than stopping at the first: the
    shape of the damage matters. One broken row with a correct chain after
    it is an edit; a break that never recovers is a removal or an insert.
    """
    problems = []
    checked = 0
    expected_previous = GENESIS

    rows = queryset.order_by('id')
    if limit:
        rows = rows[:limit]

    for entry in rows.iterator():
        checked += 1

        if entry.previous_hash != expected_previous:
            problems.append({
                'id': entry.pk,
                'created_at': entry.created_at,
                'issue': 'broken link',
                'detail': (
                    f'stored previous_hash {entry.previous_hash[:12]}… does '
                    f'not match the hash of the preceding row '
                    f'({expected_previous[:12]}…). A row was removed, '
                    f'inserted or reordered here.'
                ),
            })

        recomputed = compute_hash(entry.previous_hash, entry)
        if recomputed != entry.content_hash:
            problems.append({
                'id': entry.pk,
                'created_at': entry.created_at,
                'issue': 'content altered',
                'detail': (
                    f'row hashes to {recomputed[:12]}… but stores '
                    f'{entry.content_hash[:12]}…. The row was edited after '
                    f'it was written.'
                ),
            })

        # Continue from what the row ACTUALLY stores, not from what it
        # should have stored: otherwise one bad row makes every later row
        # look broken and the report hides where the damage really is.
        expected_previous = entry.content_hash

    return checked, problems
