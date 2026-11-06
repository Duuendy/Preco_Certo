"""Persistent database queue. Restarted jobs are reclaimed after five minutes."""
import logging
import time

from psycopg.types.json import Jsonb
from app import connect
from collector import collect
import monitoring_schedule as schedule


def process_one():
    with connect() as db:
        db.execute("UPDATE collection_jobs SET status='queued' WHERE status='running' AND started_at < now() - interval '5 minutes'")
        job = db.execute("SELECT * FROM collection_jobs WHERE status='queued' ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1").fetchone()
        if not job:
            return False
        if job['trigger']!='manual' and schedule.phase()=='paused':
            db.execute("UPDATE collection_jobs SET status='deferred',message='Monitoramento pausado do dia 1 ao 14.' WHERE id=%s",(job['id'],))
            return True
        db.execute("UPDATE collection_jobs SET status='running',started_at=now(),message='Consultando lojas públicas…' WHERE id=%s", (job['id'],))
        product = db.execute('SELECT * FROM products WHERE id=%s', (job['product_id'],)).fetchone()
        if product['archived'] or product['revision']!=job['product_revision']:
            db.execute("UPDATE collection_jobs SET status='cancelled',finished_at=now(),message='Critérios substituídos antes da consulta.' WHERE id=%s",(job['id'],))
            return True
        preferences = db.execute('SELECT * FROM preferences WHERE id=1').fetchone()
        from provider_settings import read_key
        allow_api=bool(read_key()) and schedule.reserve_api(db,job)
        product['_known_links']=db.execute('''SELECT DISTINCT o.url,o.title FROM offers o JOIN searches s ON s.id=o.search_id
            WHERE s.product_id=%s AND s.product_revision=%s LIMIT 20''',(product['id'],product['revision'])).fetchall()
        previous=db.execute("SELECT report FROM collection_jobs WHERE product_id=%s AND product_revision=%s AND status IN ('succeeded','partial') ORDER BY id DESC LIMIT 1",
                            (product['id'],product['revision'])).fetchone()
        if previous:
            known={row['url']:row for row in product['_known_links'][:10]}
            for report in previous['report']:
                for link in report.get('links',[]):
                    if link.get('url') and link.get('title'):
                        known.setdefault(link['url'],dict(url=link['url'],title=link['title']))
            product['_known_links']=list(known.values())[:20]
    offers, report = collect(product, preferences, allow_api)
    errors = any(r['status'] == 'error' for r in report)
    status = ('partial' if errors else 'succeeded') if offers else ('error' if errors else 'empty')
    message = (f'{len(offers)} ofertas encontradas. ' + ('Algumas lojas não responderam.' if errors else 'Consulta concluída.')) if offers else (
        'Não foi possível consultar todas as lojas. Tente novamente.' if errors else 'Nenhuma oferta confirmou o modelo e as características nas lojas consultadas.')
    with connect() as db:
        # Re-check the product after the network call; don't attach old-model results to an edited product.
        current = db.execute('SELECT * FROM products WHERE id=%s FOR UPDATE', (product['id'],)).fetchone()
        if current['archived'] or current['revision'] != job['product_revision']:
            status, message, offers = 'cancelled', 'Produto alterado ou arquivado durante a consulta.', []
        if offers:
            search = db.execute("""INSERT INTO searches(product_id,verified_at,notes,source,collection_job_id,product_revision)
                VALUES(%s,now(),%s,'catalogo_publico',%s,%s) RETURNING id""",
                (product['id'], 'Frete consultado nas lojas compatíveis. Total não confirmado permanece pendente. Confira variação, pagamento e entrega na loja.', job['id'], job['product_revision'])).fetchone()
            for offer in offers:
                db.execute('''INSERT INTO offers(search_id,store,url,title,price_cents,shipping_cents,
                    discount_cents,final_cents,payment,condition,imported,imported_known,shipping_known,payment_known,
                    shipping_cep,delivery_estimate,tax_cents)
                    VALUES(%s,%s,%s,%s,%s,%s,0,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                    (search['id'], offer['store'], offer['url'], offer['title'], offer['price_cents'],
                     offer.get('shipping_cents'),offer.get('final_cents'),
                     offer['payment'], offer['condition'], offer['imported'], offer['imported_known'],
                     offer.get('shipping_known',False),offer.get('payment_known', True),offer.get('shipping_cep',''),
                     offer.get('delivery_estimate',''),offer.get('tax_cents',0)))
        db.execute('''UPDATE collection_jobs SET status=%s,message=%s,report=%s,finished_at=now()
            WHERE id=%s''', (status, message, Jsonb(report), job['id']))
    if offers:
        from notifications import evaluate_search
        evaluate_search(search['id'])
    return True


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    last_schedule_check=0
    while True:
        try:
            if time.monotonic()-last_schedule_check>=60:
                with connect() as db:
                    schedule.enqueue_daily(db)
                last_schedule_check=time.monotonic()
            from notifications import deliver_one
            deliver_one()
            if not process_one():
                time.sleep(2)
        except Exception:
            logging.exception('Falha ao processar a fila; nova tentativa em breve.')
            time.sleep(5)
