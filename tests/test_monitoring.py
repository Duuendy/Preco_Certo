import io
import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from app import app, connect
from collector import collect
import monitoring_schedule as schedule
import notifications
from shipping import quote_shipping


class ScheduleAndAlertsTest(unittest.TestCase):
    def setUp(self):
        if os.environ.get('DB_NAME')!='preco_test':
            raise RuntimeError('Use the dedicated test database.')
        self.clock=patch('monitoring_schedule.now',return_value=datetime(2026,11,6,10,tzinfo=schedule.ZONE))
        self.clock.start()
        self.addCleanup(self.clock.stop)
        with connect() as db:
            db.execute('TRUNCATE offers,searches,photos,products RESTART IDENTITY CASCADE')
            db.execute("UPDATE preferences SET cep='01310100',payments=ARRAY['Pix'],imported=FALSE WHERE id=1")
        self.client=app.test_client()
        self.headers={'X-Requested-With':'PrecoCerto'}
        self.product=dict(name='Mizuno Neo Aura Knit',brand='Mizuno',model='Neo Aura Knit',category='Esporte e lazer',
                          desired_condition='Novo',requirements='',target='399',desired_size='40',preferred_colors='')
        self.id=self.client.post('/api/products',json=self.product,headers=self.headers).json['id']

    def offer(self, price, shipping, known=True, payment_known=True, url='https://example.com/product'):
        with connect() as db:
            sid=db.execute('INSERT INTO searches(product_id,verified_at) VALUES(%s,now()) RETURNING id',(self.id,)).fetchone()['id']
            db.execute('''INSERT INTO offers(search_id,store,url,title,price_cents,shipping_cents,discount_cents,
                final_cents,payment,condition,imported,shipping_known,payment_known,shipping_cep)
                VALUES(%s,'Loja',%s,'Tênis tamanho 40',%s,%s,0,%s,'Pix','Novo',FALSE,%s,%s,'01310100')''',
                (sid,url,price,shipping,price+shipping if shipping is not None else None,known,payment_known))
        return sid

    def test_phase_boundaries_and_short_months(self):
        for day,expected in [(1,'paused'),(14,'paused'),(15,'api'),(20,'api'),(21,'public'),(30,'public')]:
            self.assertEqual(schedule.phase(datetime(2026,11,day,9,tzinfo=schedule.ZONE)),expected)
        self.assertEqual(schedule.phase(datetime(2027,2,28,9,tzinfo=schedule.ZONE)),'public')
        self.assertEqual(schedule.next_run(datetime(2026,11,14,20,tzinfo=schedule.ZONE)).day,15)
        self.assertEqual(schedule.next_run(datetime(2026,11,30,20,tzinfo=schedule.ZONE)),datetime(2026,12,15,9,tzinfo=schedule.ZONE))

    def test_paused_registration_defers_but_manual_request_can_use_public_sources(self):
        product=self.client.get('/api/products').json[0]
        self.assertEqual(product['collection']['status'],'deferred')
        with connect() as db:
            self.assertEqual(schedule.enqueue_daily(db),0)
        request=self.client.post(f'/api/products/{self.id}/collect',headers=self.headers)
        self.assertEqual(request.json['status'],'queued')
        with connect() as db:
            job=db.execute('SELECT * FROM collection_jobs WHERE id=%s',(request.json['id'],)).fetchone()
            self.assertFalse(schedule.reserve_api(db,job))

    def test_daily_schedule_is_durable_once_per_product_and_date(self):
        with patch('monitoring_schedule.now',return_value=datetime(2026,11,15,8,tzinfo=schedule.ZONE)),connect() as db:
            self.assertEqual(schedule.enqueue_daily(db),0)
        with patch('monitoring_schedule.now',return_value=datetime(2026,11,15,10,tzinfo=schedule.ZONE)),connect() as db:
            self.assertEqual(schedule.enqueue_daily(db),1)
            self.assertEqual(schedule.enqueue_daily(db),0)
        with connect() as db:
            self.assertEqual(db.execute('SELECT count(*) AS n FROM collection_jobs WHERE scheduled_for IS NOT NULL').fetchone()['n'],1)

    def test_shared_budget_preserves_manual_reserve(self):
        with patch('monitoring_schedule.now',return_value=datetime(2026,11,16,9,tzinfo=schedule.ZONE)),connect() as db:
            for number in range(51):
                trigger='scheduled' if number<41 else 'manual'
                job=db.execute("INSERT INTO collection_jobs(product_id,product_revision,status,trigger) VALUES(%s,1,'succeeded',%s) RETURNING *",(self.id,trigger)).fetchone()
                result=schedule.reserve_api(db,job)
                self.assertEqual(result,number!=40)
            job=db.execute("INSERT INTO collection_jobs(product_id,product_revision,status,trigger) VALUES(%s,1,'succeeded','manual') RETURNING *",(self.id,)).fetchone()
            self.assertFalse(schedule.reserve_api(db,job))
            self.assertEqual(db.execute('SELECT sum(units) AS n FROM api_reservations').fetchone()['n'],250)

    def test_api_sources_are_not_called_in_public_mode(self):
        with patch('collector.collect_store',return_value=([],{})),patch('discovery.collect_web',return_value=([],[])) as web, \
             patch('shopping.collect_shopping') as shopping:
            collect(dict(self.product,_known_links=[]),{},False)
        shopping.assert_not_called()
        web.assert_called_once_with(dict(self.product,_known_links=[]),{},False)

    def test_goal_uses_delivered_total_and_deduplicates_unchanged_offer(self):
        sid=self.offer(38000,3000)
        self.assertFalse(notifications.evaluate_search(sid))
        sid=self.offer(35000,3000)
        self.assertTrue(notifications.evaluate_search(sid))
        self.assertFalse(notifications.evaluate_search(sid))
        sid=self.offer(35000,3000)
        self.assertFalse(notifications.evaluate_search(sid))
        with connect() as db:
            rows=db.execute('SELECT * FROM notifications').fetchall()
        self.assertEqual(len(rows),1)
        self.assertIn('Total: R$ 380,00',rows[0]['body'])

    def test_unknown_shipping_or_payment_never_reaches_target(self):
        for shipping,known,payment_known in [(None,False,True),(1000,True,False)]:
            self.assertFalse(notifications.evaluate_search(self.offer(30000,shipping,known,payment_known)))

    def test_first_expensive_offer_is_silent_and_new_record_generates_alert(self):
        self.assertFalse(notifications.evaluate_search(self.offer(50000,1000)))
        self.assertTrue(notifications.evaluate_search(self.offer(45000,1000)))
        with connect() as db:
            self.assertIn('Novo menor total registrado',db.execute('SELECT body FROM notifications').fetchone()['body'])

    def test_destination_change_cancels_pending_delivery(self):
        notifications.evaluate_search(self.offer(30000,1000))
        with connect() as db:
            db.execute("UPDATE preferences SET cep='20040002' WHERE id=1")
        config=dict(enabled=True,chat_id=123,token='dummy',linked_at='2020-01-01T00:00:00+00:00')
        with patch('notifications.read_config',return_value=config),patch('notifications.telegram_call') as send:
            notifications.deliver_one()
        send.assert_not_called()

    def test_successful_alert_delivery_is_not_repeated(self):
        notifications.evaluate_search(self.offer(30000,1000))
        config=dict(enabled=True,chat_id=123,token='dummy',linked_at='2020-01-01T00:00:00+00:00')
        with patch('notifications.read_config',return_value=config),patch('notifications.telegram_call') as send:
            self.assertTrue(notifications.deliver_one())
            self.assertFalse(notifications.deliver_one())
        self.assertEqual(send.call_count,1)


class ShippingAndTelegramTest(unittest.TestCase):
    def test_shipping_uses_exact_sku_seller_cep_and_ignores_pickup(self):
        response=MagicMock()
        response.__enter__.return_value.read.return_value=json.dumps(dict(items=[dict(availability='available',sellingPrice=35000,tax=0)],
            logisticsInfo=[dict(itemIndex=0,slas=[dict(deliveryChannel='pickup-in-point',price=0),
                dict(deliveryChannel='delivery',price=3000,shippingEstimate='3bd')])])).encode()
        with patch('shipping.urlopen',return_value=response) as remote:
            result=quote_shipping('https://shop.example','sku40','seller1','01310100',35000)
        payload=json.loads(remote.call_args[0][0].data)
        self.assertEqual(payload['items'],[dict(id='sku40',quantity=1,seller='seller1')])
        self.assertEqual(payload['postalCode'],'01310100')
        self.assertEqual(result['final_cents'],38000)
        with patch('shipping.urlopen') as remote:
            self.assertIsNone(quote_shipping('https://shop.example','sku40','seller1','',35000))
            remote.assert_not_called()

    def test_shipping_failure_never_becomes_free_shipping(self):
        with patch('shipping.urlopen',side_effect=TimeoutError):
            self.assertIsNone(quote_shipping('https://shop.example','sku','seller','01310100',100))

    def test_pairing_requires_exclusive_start_code_and_does_not_expose_token(self):
        token='123456:abcdefghijklmnopqrstuvwxyz123456'
        with tempfile.TemporaryDirectory() as directory,patch('notifications.config_path',return_value=Path(directory)/'telegram.json'):
            with patch('notifications.telegram_call',return_value=dict(is_bot=True,username='PrecoCertoTestBot')):
                prepared=notifications.prepare(token)
            self.assertNotIn(token,json.dumps(prepared))
            config=notifications.read_config()
            wrong=dict(message=dict(text='/start wrong',date=time.time(),chat=dict(id=123,type='private'),**{'from':dict(id=123)}))
            with patch('notifications.telegram_call',return_value=[wrong]):
                with self.assertRaises(ValueError):
                    notifications.link_chat()
            correct=dict(message=dict(text='/start '+config['nonce'],date=time.time(),chat=dict(id=123,type='private'),**{'from':dict(id=123)}))
            with patch('notifications.telegram_call',return_value=[correct]) as remote, \
                 patch('notifications.time.time',return_value=config['pairing_expires']+60):
                linked=notifications.link_chat()
            self.assertTrue(linked['linked'])
            self.assertNotIn(token,json.dumps(linked))
            self.assertEqual(remote.call_args[0][1],'getUpdates')
            self.assertEqual(Path(directory,'telegram.json').stat().st_mode & 0o777,0o600)
