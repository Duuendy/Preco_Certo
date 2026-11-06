from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ZONE = ZoneInfo('America/Sao_Paulo')


def now():
    return datetime.now(ZONE)


def phase(moment=None):
    day = (moment or now()).astimezone(ZONE).day
    return 'paused' if day < 15 else 'api' if day <= 20 else 'public'


def next_run(moment=None):
    current = (moment or now()).astimezone(ZONE)
    candidate = current.replace(hour=9, minute=0, second=0, microsecond=0)
    if candidate <= current:
        candidate += timedelta(days=1)
    while candidate.day < 15:
        candidate += timedelta(days=1)
    return candidate


def enqueue_daily(db):
    current = now()
    if phase(current) == 'paused' or current.hour < 9:
        return 0
    db.execute('SELECT pg_advisory_xact_lock(781204)')
    products = db.execute('SELECT id,revision FROM products WHERE NOT archived ORDER BY id').fetchall()
    if products:
        shift = (current.day - 15) % len(products)
        products = products[shift:] + products[:shift]
    count = 0
    for product in products:
        exists = db.execute('SELECT id FROM collection_jobs WHERE product_id=%s AND scheduled_for=%s',
                            (product['id'], current.date())).fetchone()
        if exists:
            continue
        active = db.execute("""SELECT id FROM collection_jobs WHERE product_id=%s AND product_revision=%s
            AND (status IN ('queued','running') OR (trigger='product_saved'
            AND (created_at AT TIME ZONE 'America/Sao_Paulo')::date=%s
            AND status IN ('succeeded','partial','empty','error'))) ORDER BY id LIMIT 1""",
            (product['id'],product['revision'],current.date())).fetchone()
        if active:
            db.execute('UPDATE collection_jobs SET scheduled_for=%s WHERE id=%s', (current.date(),active['id']))
            continue
        db.execute("UPDATE collection_jobs SET status='cancelled',message='Substituída pela consulta diária.' WHERE product_id=%s AND status='deferred'",(product['id'],))
        db.execute("""INSERT INTO collection_jobs(product_id,product_revision,trigger,scheduled_for)
            VALUES(%s,%s,'scheduled',%s) ON CONFLICT DO NOTHING""", (product['id'],product['revision'],current.date()))
        count += 1
    return count


def reserve_api(db, job):
    if phase() != 'api':
        return False
    month = now().date().replace(day=1)
    db.execute('SELECT pg_advisory_xact_lock(781205)')
    existing = db.execute('SELECT job_id FROM api_reservations WHERE job_id=%s',(job['id'],)).fetchone()
    if existing:
        return True
    reserved = db.execute('SELECT coalesce(sum(units),0) AS n FROM api_reservations WHERE month=%s',(month,)).fetchone()['n']
    limit = 250 if job['trigger'] == 'manual' else 200
    if reserved + 5 > limit:
        return False
    db.execute('INSERT INTO api_reservations(job_id,month,units) VALUES(%s,%s,5)',(job['id'],month))
    return True
