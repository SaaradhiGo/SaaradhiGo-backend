import json, os, time
import redis
from django.db import connection
out = {'t': time.strftime('%H:%M:%S')}
with connection.cursor() as c:
    c.execute("select state, count(*) from pg_stat_activity where datname = current_database() group by state")
    out['pg_conns'] = dict((s or 'none', n) for s, n in c.fetchall())
    c.execute("select xact_commit, xact_rollback, tup_inserted, tup_updated, blks_read, blks_hit, deadlocks from pg_stat_database where datname = current_database()")
    out['pg_db'] = list(c.fetchone())
    c.execute("select count(*) from pg_stat_activity where wait_event_type = 'Lock'")
    out['pg_lock_waits'] = c.fetchone()[0]
    c.execute("select coalesce(max(extract(epoch from now() - query_start)), 0) from pg_stat_activity where state = 'active' and datname = current_database()")
    out['pg_longest_active_s'] = round(float(c.fetchone()[0]), 2)
r = redis.from_url(os.environ['REDIS_URL'])
i = r.info()
out['redis'] = {k: i.get(k) for k in ('connected_clients', 'instantaneous_ops_per_sec', 'used_memory_human', 'total_commands_processed', 'rejected_connections', 'blocked_clients', 'evicted_keys')}
out['celery_q'] = {q: r.llen(q) for q in ('celery', 'dispatch', 'default') if r.exists(q)}
print('LTMON ' + json.dumps(out))
