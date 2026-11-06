import io
import unittest
from datetime import datetime, timedelta, timezone

from openpyxl import load_workbook
from PIL import Image

from app import app, cents, connect


class PriceHistoryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Use a separate database; never delete records from the application DB.
        import os
        if os.environ.get('DB_NAME') != 'preco_test':
            raise RuntimeError('Run tests with DB_NAME=preco_test.')
        cls.client = app.test_client()
        cls.headers = {'X-Requested-With': 'PrecoCerto'}

    def setUp(self):
        with connect() as db:
            db.execute('TRUNCATE offers, searches, photos, products RESTART IDENTITY CASCADE')
        self.product = dict(name='Televisão', brand='Marca', model='TV55', category='Eletrônicos',
                            requirements='55 polegadas, 4K', desired_condition='Novo', target='2500.00')
        response = self.client.post('/api/products', json=self.product, headers=self.headers)
        self.assertEqual(response.status_code, 201)
        self.id = response.json['id']

    def search(self, base_price, days, **overrides):
        offer = dict(store='Loja', url='https://example.com/tv', price=base_price, shipping='30.00',
                     discount='100.00', payment='Pix', condition='Novo', imported=False)
        offer.update(overrides)
        return self.client.post(f'/api/products/{self.id}/searches', json={
            'verified_at': (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(),
            'notes': 'Verificação manual', 'offers': [offer]}, headers=self.headers)

    def test_history_keeps_prior_searches_and_chronological_order(self):
        self.assertEqual(self.search('2400.01', 1).status_code, 201)
        self.assertEqual(self.search('2700.00', 3).status_code, 201)
        product = self.client.get('/api/products').json[0]
        self.assertEqual([h['best_cents'] for h in product['history']], [263000, 233001])
        self.assertEqual(product['history'][1]['best_cents'] - product['history'][0]['best_cents'], -29999)
        self.product['name'] = 'Televisão da sala'
        self.client.put(f'/api/products/{self.id}', json=self.product, headers=self.headers)
        self.assertEqual(len(self.client.get(f'/api/products/{self.id}/offers').json), 2)

    def test_research_groups_stores_and_uses_lowest_final_price(self):
        entries = [dict(store='A', url='https://example.com/a', price='100', shipping='20', discount='0', payment='Pix'),
                   dict(store='B', url='https://example.com/b', price='110', shipping='0', discount='10', payment='Pix')]
        result = self.client.post(f'/api/products/{self.id}/searches', json={
            'verified_at': (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(), 'offers': entries}, headers=self.headers)
        self.assertEqual(result.status_code, 201)
        history = self.client.get('/api/products').json[0]['history']
        self.assertEqual((history[0]['offer_count'], history[0]['best_cents']), (2, 10000))

    def test_invalid_offers_are_atomic(self):
        for override in [dict(discount='9999'), dict(price='NaN'), dict(url='javascript:alert(1)'), dict(price='1.001'), dict(imported='false')]:
            self.assertEqual(self.search('100', 1, **override).status_code, 400)
        self.assertEqual(self.client.get('/api/products').json[0]['history'], [])

    def test_missing_product_and_future_search(self):
        result = self.client.post('/api/products/999/searches', json={
            'verified_at': (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
            'offers': [dict(store='A', url='https://example.com', price='10', payment='Pix')]}, headers=self.headers)
        self.assertEqual(result.status_code, 404)
        self.assertEqual(self.search('200', -2).status_code, 400)

    def test_archiving_preserves_history(self):
        self.search('200', 1)
        self.assertEqual(self.client.patch(f'/api/products/{self.id}/archive', json={'archived': True}, headers=self.headers).status_code, 200)
        self.assertEqual(self.search('210', 0).status_code, 400)
        self.assertEqual(len(self.client.get(f'/api/products/{self.id}/offers').json), 1)
        self.client.patch(f'/api/products/{self.id}/archive', json={'archived': False}, headers=self.headers)
        self.assertEqual(self.search('210', 0).status_code, 201)

    def test_preferences_validation_and_persistence(self):
        preferences = dict(cep='01310-100', payments=['Pix'], imported=True, used=False)
        self.assertEqual(self.client.put('/api/preferences', json=preferences, headers=self.headers).status_code, 200)
        self.assertEqual(self.client.get('/api/preferences').json['cep'], '01310100')
        preferences['payments'] = []
        self.assertEqual(self.client.put('/api/preferences', json=preferences, headers=self.headers).status_code, 400)

    def test_real_excel_has_numeric_money_and_no_formula_injection(self):
        self.product['name'] = '=HYPERLINK("https://example.com")'
        self.client.put(f'/api/products/{self.id}', json=self.product, headers=self.headers)
        self.search('1234.56', 1)
        response = self.client.get('/api/export.xlsx')
        self.assertEqual(response.status_code, 200)
        workbook = load_workbook(io.BytesIO(response.data))
        self.assertEqual(workbook.sheetnames, ['Produtos', 'Ofertas', 'Preferências'])
        self.assertEqual(workbook['Produtos']['B2'].data_type, 's')
        self.assertEqual(workbook['Ofertas']['J2'].value, 1164.56)

    def test_photos_are_validated_and_served(self):
        image = io.BytesIO()
        Image.new('RGB', (12, 12), 'green').save(image, 'PNG')
        image.seek(0)
        result = self.client.post(f'/api/products/{self.id}/photos', data={'photos': (image, 'photo.png')}, headers=self.headers)
        self.assertEqual(result.status_code, 201)
        product = self.client.get('/api/products').json[0]
        filename = product['photos'][0]['filename']
        response = self.client.get(f'/photos/{filename}')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, 'image/jpeg')
        response.close()
        result = self.client.post(f'/api/products/{self.id}/photos', data={'photos': (io.BytesIO(b'not an image'), 'evil.jpg')}, headers=self.headers)
        self.assertEqual(result.status_code, 400)

    def test_writes_require_same_origin_custom_header(self):
        self.assertEqual(self.client.post('/api/products', json=self.product).status_code, 403)
        response = self.client.get('/')
        self.assertIn("frame-ancestors 'none'", response.headers['Content-Security-Policy'])
        response.close()

    def test_cents_avoid_float_rounding(self):
        self.assertEqual(cents('0.29'), 29)
        for invalid in ['NaN', 'Infinity', '-1', '1.234', True]:
            with self.assertRaises(ValueError):
                cents(invalid)


if __name__ == '__main__':
    unittest.main()
