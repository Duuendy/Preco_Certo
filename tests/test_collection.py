import io
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from openpyxl import load_workbook
from app import app, connect, price_summary
from collector import collect_store, matches
from worker import process_one


def catalog_product():
    return dict(productName='Tênis Mizuno Neo Aura Knit', brand='Mizuno', Origem=['Nacional'],
                link='https://www.mizuno.com.br/tenis/p', allSpecifications=[], items=[
                    dict(itemId='123', nameComplete='Tênis Mizuno Neo Aura Knit 40 Azul',
                         sellers=[dict(sellerName='Mizuno', commertialOffer=dict(Price=599.99, AvailableQuantity=3,
                             Installments=[dict(NumberOfInstallments=1, TotalValuePlusInterestRate=599.99,
                                                PaymentSystemGroupName='creditCardPaymentGroup')]))])])


class CollectionTest(unittest.TestCase):
    def setUp(self):
        if os.environ.get('DB_NAME') != 'preco_test':
            raise RuntimeError('Use the dedicated test database.')
        clock=patch('monitoring_schedule.now',return_value=datetime(2026,11,16,9,tzinfo=timezone(timedelta(hours=-3))))
        clock.start()
        self.addCleanup(clock.stop)
        self.client = app.test_client()
        self.headers = {'X-Requested-With': 'PrecoCerto'}
        with connect() as db:
            db.execute('TRUNCATE offers,searches,photos,products RESTART IDENTITY CASCADE')
            db.execute("UPDATE preferences SET payments=ARRAY['Cartão de crédito'],imported=FALSE WHERE id=1")
        self.product = dict(name='Mizuno Neo Aura Knit', brand='Mizuno', model='Neo Aura Knit',
                            requirements='', desired_condition='Novo', target='650', category='Esporte e lazer')
        self.id = self.client.post('/api/products', json=self.product, headers=self.headers).json['id']

    def test_product_registration_queues_exactly_one_job(self):
        product = self.client.get('/api/products').json[0]
        self.assertEqual(product['collection']['status'], 'queued')
        first = self.client.post(f'/api/products/{self.id}/collect', headers=self.headers)
        second = self.client.post(f'/api/products/{self.id}/collect', headers=self.headers)
        self.assertEqual(first.json['id'], second.json['id'])
        with connect() as db:
            self.assertEqual(db.execute('SELECT count(*) AS n FROM collection_jobs').fetchone()['n'], 1)

    def test_editing_variant_updates_existing_product_and_queues_refined_research(self):
        self.client.post(f'/api/products/{self.id}/searches', json={
            'verified_at': (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
            'offers': [dict(store='Loja',url='https://example.com',price='550',payment='Pix')]}, headers=self.headers)
        self.product.update(desired_size='37', preferred_colors='Cinza e marinho, Preto')
        result = self.client.put(f'/api/products/{self.id}', json=self.product, headers=self.headers)
        self.assertEqual(result.status_code, 200)
        products = self.client.get('/api/products').json
        self.assertEqual(len(products), 1)
        self.assertEqual(products[0]['id'], self.id)
        self.assertEqual(products[0]['desired_size'], '37')
        self.assertEqual(products[0]['preferred_colors'], 'Cinza e marinho, Preto')
        self.assertIn('37 ("Cinza e marinho" OR "Preto")', products[0]['search_query'])
        self.assertEqual(products[0]['collection']['status'], 'queued')
        self.assertEqual(products[0]['revision'], 2)
        self.assertEqual(products[0]['history'], [])
        self.assertEqual(len(self.client.get(f'/api/products/{self.id}/offers').json), 1)

    def test_catalog_variant_filter_checks_each_sku_and_accepts_alternative_colors(self):
        candidate = catalog_product()
        item = candidate['items'][0]
        self.product.update(desired_size='37', preferred_colors='Cinza e marinho, Preto')
        item.update(Tamanho=['37'], Cor=['Cinza/Marinho'])
        self.assertTrue(matches(self.product, candidate, item))
        item['Cor'] = ['Preto']
        self.assertTrue(matches(self.product, candidate, item))
        item['Cor'] = ['Azul']
        self.assertFalse(matches(self.product, candidate, item))
        item['Cor'] = ['Preto']
        item['Tamanho'] = ['38']
        self.assertFalse(matches(self.product, candidate, item))

    def test_variant_fields_reject_invalid_values_without_updating_product(self):
        for changes in [dict(desired_size=['37']), dict(preferred_colors='x' * 161)]:
            response = self.client.put(f'/api/products/{self.id}',json={**self.product,**changes},headers=self.headers)
            self.assertEqual(response.status_code,400)
        product = self.client.get('/api/products').json[0]
        self.assertEqual(product['desired_size'], '')
        self.assertEqual(product['revision'], 1)

    def test_match_rejects_wrong_model_and_unconfirmed_requirements(self):
        candidate = catalog_product()
        item = candidate['items'][0]
        self.assertTrue(matches(self.product, candidate, item))
        self.product['model'] = 'Neo Zen'
        self.assertFalse(matches(self.product, candidate, item))
        self.product['model'] = 'Neo Aura Knit'
        self.product['requirements'] = 'Azul 40'
        self.assertTrue(matches(self.product, candidate, item))
        self.product['requirements'] = 'Azul 43'
        self.assertFalse(matches(self.product, candidate, item))

    def test_collector_filters_stock_and_payment(self):
        candidate = catalog_product()
        preferences = dict(payments=['Cartão de crédito'], imported=False)
        with patch('collector.fetch_catalog', return_value=[candidate]):
            offers, report = collect_store(('Mizuno', 'https://www.mizuno.com.br', 'mizuno'), self.product, preferences)
            self.assertEqual(offers[0]['price_cents'], 59999)
            self.assertIn('idsku=123', offers[0]['url'])
            self.assertEqual(report['status'], 'ok')
            preferences['payments'] = ['Boleto']
            self.assertEqual(collect_store(('Mizuno', 'https://www.mizuno.com.br', 'mizuno'), self.product, preferences)[0], [])
            preferences['payments'] = ['Cartão de crédito']
            candidate['items'][0]['sellers'][0]['commertialOffer']['AvailableQuantity'] = 0
            self.assertEqual(collect_store(('Mizuno', 'https://www.mizuno.com.br', 'mizuno'), self.product, preferences)[0], [])

    def test_blocked_source_is_an_error_never_a_zero_price(self):
        with patch('collector.fetch_catalog', side_effect=TimeoutError):
            offers, report = collect_store(('Mizuno', 'https://www.mizuno.com.br', 'mizuno'), self.product,
                                          dict(payments=['Pix'], imported=False))
        self.assertEqual(offers, [])
        self.assertEqual(report['status'], 'error')

    def test_worker_saves_real_source_unknown_shipping_and_excel_blanks(self):
        offer = dict(store='Mizuno', url='https://www.mizuno.com.br/tenis/p?idsku=123', title='Aura Knit 40 Azul',
                     price_cents=59999, payment='Cartão de crédito', condition='Novo', imported=False, imported_known=True)
        with patch('worker.collect', return_value=([offer], [dict(store='Mizuno', status='ok', count=1)])):
            self.assertTrue(process_one())
        product = self.client.get('/api/products').json[0]
        self.assertEqual(product['collection']['status'], 'succeeded')
        self.assertEqual(product['price_summary']['verdict'], 'first')
        self.assertEqual(product['price_summary']['current_cents'], 59999)
        result = self.client.get(f'/api/products/{self.id}/offers').json[0]
        self.assertEqual(result['source'], 'catalogo_publico')
        self.assertIsNone(result['shipping_cents'])
        self.assertIsNone(result['final_cents'])
        sheet = load_workbook(io.BytesIO(self.client.get('/api/export.xlsx').data))['Ofertas']
        self.assertIsNone(sheet['H2'].value)
        self.assertIsNone(sheet['J2'].value)
        self.assertEqual(sheet['N2'].value, 'catalogo_publico')

    def test_empty_collection_does_not_create_a_fake_search(self):
        with patch('worker.collect', return_value=([], [dict(store='Mizuno', status='empty')])):
            process_one()
        product = self.client.get('/api/products').json[0]
        self.assertEqual(product['collection']['status'], 'empty')
        self.assertEqual(product['history'], [])
        self.assertIsNone(product['price_summary']['current_cents'])

    def test_product_changed_during_collection_discards_results(self):
        def changed(*args):
            self.product['model'] = 'Neo Zen'
            self.client.put(f'/api/products/{self.id}', json=self.product, headers=self.headers)
            return [], []
        with patch('worker.collect', side_effect=changed):
            process_one()
        with connect() as db:
            rows = db.execute('SELECT status FROM collection_jobs ORDER BY id').fetchall()
        self.assertEqual([row['status'] for row in rows], ['cancelled', 'queued'])

    def test_changed_model_preserves_old_offers_but_starts_a_separate_comparison(self):
        self.client.post(f'/api/products/{self.id}/searches', json={
            'verified_at': (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
            'offers': [dict(store='Loja', url='https://example.com', price='550', payment='Pix')]}, headers=self.headers)
        self.product['model'] = 'Neo Zen'
        self.client.put(f'/api/products/{self.id}', json=self.product, headers=self.headers)
        product = self.client.get('/api/products').json[0]
        self.assertEqual(product['price_summary']['verdict'], 'unavailable')
        self.assertEqual(len(self.client.get(f'/api/products/{self.id}/offers').json), 1)

    def test_comparison_uses_previous_record_low_not_last_search_or_today(self):
        for current, verdict, delta in [(50000, 'lower', -10000), (60000, 'equal', 0), (80000, 'higher', 20000)]:
            summary = price_summary([dict(best_price_cents=60000), dict(best_price_cents=70000), dict(best_price_cents=current)])
            self.assertEqual(summary['verdict'], verdict)
            self.assertEqual(summary['previous_low_cents'], 60000)
            self.assertEqual(summary['difference_cents'], delta)
        self.assertEqual(price_summary([dict(best_price_cents=50000)])['verdict'], 'first')
        self.assertIsNone(price_summary([])['difference_percent'])

    def test_historical_data_is_not_changed_when_new_collection_fails(self):
        self.client.post(f'/api/products/{self.id}/searches', json={
            'verified_at': (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
            'offers': [dict(store='Loja anterior', url='https://example.com', price='550', shipping='40', discount='0', payment='Pix')]}, headers=self.headers)
        with patch('worker.collect', return_value=([], [dict(store='Mizuno', status='error')])):
            process_one()
        product = self.client.get('/api/products').json[0]
        self.assertEqual(len(product['history']), 1)
        self.assertEqual(product['history'][0]['best_price_cents'], 55000)
        self.assertEqual(product['collection']['status'], 'error')
