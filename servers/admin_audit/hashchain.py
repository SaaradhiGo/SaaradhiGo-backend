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

ROWS THAT PREDATE THE CHAIN
---------------------------
The chain was added (migration 0003) to a table that already had rows. Those
rows carry no hash, and they must not be given one after the fact: the table
is append-only, and a row rewritten to look protected is exactly the kind of
edit the chain exists to expose. Found on QA on 2026-10-27, where
`verify_audit_chain` reported every one of the 20 pre-chain rows as "content
altered" -- an alarm that would have been permanently on in production, and
a permanent alarm is one people learn to ignore.

So instead of editing them, ONE row vouches for them: a CHAIN_ANCHOR row,
appended once by migration 0005, that records how many unhashed rows came
before it, the highest id among them, and a SHA-256 over their canonical
content in id order. The anchor is itself hashed into the chain, so it
cannot be altered either. From then on:

  * the verifier accepts unhashed rows ONLY as one contiguous run at the
    start of the table, and only if an anchor vouches for them -- it
    recomputes the digest over the run and compares;
  * an unhashed row after the chain has started is a failure, whatever it
    says, because nothing legitimate writes one;
  * an anchor whose count, max id or digest disagrees with the run is a
    failure: a pre-chain row was edited or removed.

The anchor is appended at the moment the migration runs, which on a live
database may be after some hashed rows already exist (QA had rows 21+
before any anchor could be written). That is fine: the anchor commits to the
pre-chain run through its digest and to everything between through its
`previous_hash`, like any other row.
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

#: The row that starts the chain on a table that already had rows.
#: See "ROWS THAT PREDATE THE CHAIN" above and migration 0005.
ANCHOR_ACTION = 'chain_anchor'
ANCHOR_TARGET_TYPE = 'audit_chain'


class ChainStateError(RuntimeError):
    """The table is not in a state an anchor can honestly describe."""


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


def is_anchor(entry):
    return (entry.action == ANCHOR_ACTION
            and entry.target_type == ANCHOR_TARGET_TYPE)


def pre_chain_digest(rows):
    """SHA-256 over the canonical content of `rows`, in the order given.

    Newline-terminated per row so two different splits of the same bytes
    cannot collide. The verifier and `write_anchor` both use this; there is
    one definition of what the anchor covers.
    """
    digest = hashlib.sha256()
    for row in rows:
        digest.update(canonical_content(row).encode('utf-8'))
        digest.update(b'\n')
    return digest.hexdigest()


def write_anchor(Model, *, label):
    """Append the CHAIN_ANCHOR row for the unhashed rows already in the table.

    Returns the new row, or None when there is nothing to anchor: a table
    that never had pre-chain rows needs no anchor and gets none.

    Works on the real model and on a migration's historical model alike --
    the hashes are computed here rather than left to `save()`, because the
    historical model has no custom `save()`.

    Refuses, rather than vouching for something it cannot:
      * if an anchor already exists -- the chain starts once;
      * if an unhashed row sits AFTER a hashed one -- that is not a
        pre-chain run, it is damage, and an anchor must not paper over it.
    """
    from django.utils import timezone

    if Model.objects.filter(action=ANCHOR_ACTION,
                            target_type=ANCHOR_TARGET_TYPE).exists():
        raise ChainStateError('The audit chain already has an anchor row.')

    unhashed = list(Model.objects.filter(content_hash='').order_by('id'))
    if not unhashed:
        return None

    first_hashed_id = (Model.objects.exclude(content_hash='')
                       .order_by('id').values_list('id', flat=True).first())
    if first_hashed_id is not None and unhashed[-1].pk > first_hashed_id:
        raise ChainStateError(
            f'Unhashed row {unhashed[-1].pk} comes after hashed row '
            f'{first_hashed_id}. Pre-chain rows must be one leading run; '
            f'refusing to anchor over what looks like tampering.'
        )

    tail = (Model.objects.order_by('-id')
            .values_list('content_hash', flat=True).first())
    row = Model(
        actor=None,
        actor_label=label,
        action=ANCHOR_ACTION,
        target_type=ANCHOR_TARGET_TYPE,
        target_id='',
        before={},
        after={
            'pre_chain_rows': len(unhashed),
            'pre_chain_first_id': unhashed[0].pk,
            'pre_chain_max_id': unhashed[-1].pk,
            'pre_chain_digest': pre_chain_digest(unhashed),
            'digest': 'sha256 over canonical_content of each row in id '
                      'order, newline-terminated',
        },
        reason=(
            f'Hash chain started. The {len(unhashed)} row(s) before this one '
            f'predate the chain and carry no hash; they are protected by the '
            f'digest in this row, not by being rewritten.'
        ),
        created_at=timezone.now(),
    )
    row.previous_hash = tail or GENESIS
    row.content_hash = compute_hash(row.previous_hash, row)
    row.save(force_insert=True)
    return row


def inspect_chain(queryset, *, limit=None):
    """Walk the log oldest-first and report every break.

    Returns a dict:
      checked    rows examined
      problems   list of dicts (id, created_at, issue, detail); empty is good
      pre_chain  None, or {rows, first_id, last_id, anchor_id} describing
                 the leading run of unhashed rows and the anchor that
                 vouches for them (anchor_id is None if none was found)

    Deliberately reports EVERY break rather than stopping at the first: the
    shape of the damage matters. One broken row with a correct chain after
    it is an edit; a break that never recovers is a removal or an insert.
    """
    problems = []
    checked = 0
    expected_previous = GENESIS

    # The leading run of rows written before the chain existed.
    pre_count = 0
    pre_first_id = pre_last_id = None
    pre_last_created = None
    pre_digest = hashlib.sha256()
    chain_started = False
    anchor = None

    rows = queryset.order_by('id')
    if limit:
        rows = rows[:limit]

    def _problem(entry, issue, detail):
        problems.append({'id': entry.pk, 'created_at': entry.created_at,
                         'issue': issue, 'detail': detail})

    for entry in rows.iterator():
        checked += 1

        if not entry.content_hash:
            if chain_started:
                _problem(entry, 'unhashed row', (
                    'This row carries no hash but was written after the '
                    'chain started. Nothing legitimate writes such a row; '
                    'it was inserted outside the application.'
                ))
                continue
            pre_count += 1
            if pre_first_id is None:
                pre_first_id = entry.pk
            pre_last_id = entry.pk
            pre_last_created = entry.created_at
            pre_digest.update(canonical_content(entry).encode('utf-8'))
            pre_digest.update(b'\n')
            continue

        if entry.previous_hash != expected_previous:
            _problem(entry, 'broken link', (
                f'stored previous_hash {entry.previous_hash[:12]}… does '
                f'not match the hash of the preceding row '
                f'({expected_previous[:12]}…). A row was removed, '
                f'inserted or reordered here.'
            ))

        recomputed = compute_hash(entry.previous_hash, entry)
        if recomputed != entry.content_hash:
            _problem(entry, 'content altered', (
                f'row hashes to {recomputed[:12]}… but stores '
                f'{entry.content_hash[:12]}…. The row was edited after '
                f'it was written.'
            ))

        if is_anchor(entry):
            if anchor is not None:
                _problem(entry, 'second anchor', (
                    f'The chain already started at row {anchor.pk}; a '
                    f'second CHAIN_ANCHOR row is not something the '
                    f'application writes.'
                ))
            else:
                anchor = entry
                stored = entry.after or {}
                actual = {
                    'pre_chain_rows': pre_count,
                    'pre_chain_max_id': pre_last_id,
                    'pre_chain_digest': pre_digest.hexdigest(),
                }
                claimed = {key: stored.get(key) for key in actual}
                if claimed != actual:
                    _problem(entry, 'anchor mismatch', (
                        f'the anchor vouches for {claimed["pre_chain_rows"]} '
                        f'pre-chain row(s) up to id '
                        f'{claimed["pre_chain_max_id"]} with digest '
                        f'{str(claimed["pre_chain_digest"])[:12]}…; the table '
                        f'holds {actual["pre_chain_rows"]} up to id '
                        f'{actual["pre_chain_max_id"]} with digest '
                        f'{actual["pre_chain_digest"][:12]}…. A pre-chain row '
                        f'was edited or removed.'
                    ))

        chain_started = True
        # Continue from what the row ACTUALLY stores, not from what it
        # should have stored: otherwise one bad row makes every later row
        # look broken and the report hides where the damage really is.
        expected_previous = entry.content_hash

    # A leading run with nobody vouching for it. Skipped only when `limit`
    # cut the walk off inside the run, where the anchor could not have been
    # reached yet.
    walked_past_the_run = not (limit and pre_count == checked)
    if pre_count and anchor is None and walked_past_the_run:
        problems.append({
            'id': pre_last_id, 'created_at': pre_last_created,
            'issue': 'unanchored pre-chain rows',
            'detail': (
                f'{pre_count} row(s) (ids {pre_first_id}–{pre_last_id}) carry '
                f'no hash and no CHAIN_ANCHOR row vouches for them. Apply '
                f'admin_audit migration 0005; until then these rows are '
                f'unverified.'
            ),
        })

    pre_chain = None
    if pre_count:
        pre_chain = {
            'rows': pre_count, 'first_id': pre_first_id,
            'last_id': pre_last_id,
            'anchor_id': anchor.pk if anchor is not None else None,
        }
    return {'checked': checked, 'problems': problems, 'pre_chain': pre_chain}


def verify_chain(queryset, *, limit=None):
    """`(checked, problems)` -- see `inspect_chain` for the full report."""
    report = inspect_chain(queryset, limit=limit)
    return report['checked'], report['problems']
