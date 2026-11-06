import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import app
from discovery import UnsafeURL, direct_product_url, inspect_page, public_addresses, search_pages
from provider_settings import free_account


class PublicDiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.product = dict(brand='Mizuno', model='Neo Aura Knit', requirements='', desired_condition='Novo')
        self.link = dict(title='Tênis Mizuno Neo Aura Knit Azul', url='https://shop.example/produto/neo-aura-knit')
        self.node = dict(**{'@type':'Product'}, name='Mizuno Neo Aura Knit', brand={'name':'Mizuno'},
                         offers={'@type':'Offer', 'price':499.90, 'priceCurrency':'BRL',
                                 'availability':'https://schema.org/InStock', 'itemCondition':'https://schema.org/NewCondition'})

    def inspect(self):
        body = '<script type="application/ld+json">' + json.dumps(self.node) + '</script>'
        with patch('discovery.read_public', return_value=(body, self.link['url'])):
            return inspect_page(self.link, self.product, dict(imported=False, payments=['Pix']))

    def test_verified_page_keeps_direct_merchant_link_and_unknown_payment(self):
        offer, page = self.inspect()
        self.assertEqual(offer['url'], self.link['url'])
        self.assertEqual(offer['price_cents'], 49990)
        self.assertFalse(offer['payment_known'])
        self.assertEqual(page['status'], 'verified')

    def test_unconfirmed_size_or_color_never_enters_refined_history(self):
        self.product.update(desired_size='37', preferred_colors='Cinza e marinho, Preto')
        self.assertIsNone(self.inspect()[0])
        self.node.update(size='38', color='Preto')
        self.assertIsNone(self.inspect()[0])
        self.node.update(size='37', color='Cinza e marinho')
        self.assertEqual(self.inspect()[0]['price_cents'], 49990)

    def test_offer_sku_link_is_preserved_and_external_aggregator_is_not_verified(self):
        self.node['offers']['url'] = self.link['url'] + '?sku=40-azul'
        self.assertEqual(self.inspect()[0]['url'], self.link['url'] + '?sku=40-azul')
        self.node['offers']['url'] = 'https://other.example/product/another-model'
        self.assertIsNone(self.inspect()[0])

    def test_aggregate_prices_and_out_of_stock_do_not_enter_history(self):
        self.node['offers']['availability'] = 'https://schema.org/OutOfStock'
        self.assertIsNone(self.inspect()[0])
        self.node['offers'] = {'@type':'AggregateOffer', 'lowPrice':99.99, 'priceCurrency':'BRL'}
        self.assertIsNone(self.inspect()[0])

    def test_wrong_model_in_description_does_not_validate_product(self):
        self.node['name'] = 'Mizuno Neo Zen'
        self.node['description'] = 'Conheça também Neo Aura Knit'
        self.assertIsNone(self.inspect()[0])

    def test_unconfirmed_page_keeps_link_but_no_price(self):
        with patch('discovery.read_public', side_effect=TimeoutError):
            offer, page = inspect_page(self.link, self.product, {})
        self.assertIsNone(offer)
        self.assertEqual(page['url'], self.link['url'])
        self.assertNotIn('price_cents', page)

    def test_private_network_destinations_are_rejected_not_published(self):
        for address in ['127.0.0.1', '10.0.0.1', '169.254.169.254', '::1']:
            with patch('discovery.socket.getaddrinfo', return_value=[(None,None,None,None,(address,443))]):
                with self.assertRaises(UnsafeURL):
                    public_addresses('shop.example')
        with patch('discovery.read_public', side_effect=UnsafeURL):
            self.assertEqual(inspect_page(self.link, self.product, {}), (None,None))

    def test_discovery_filters_categories_searches_and_aggregators(self):
        self.assertTrue(direct_product_url(self.link['url']))
        for url in ['javascript:alert(1)', 'http://shop.example/product/test', 'https://shop.example/',
                    'https://shop.example/busca/neo-aura', 'https://google.com/shopping/product/123',
                    'https://shop.example/longpath?q=neo']:
            self.assertFalse(direct_product_url(url))

    def test_search_api_returns_matching_merchant_links_without_snippet_prices(self):
        payload = {'organic_results':[dict(title=self.link['title'], link=self.link['url'], snippet='R$ 99,90'),
                                      dict(title='Mizuno Neo Zen', link='https://other.example/neo-zen')]}
        with patch('provider_settings.read_key', return_value='test-key'), patch('provider_settings.free_account'), \
             patch('discovery.read_public', return_value=(json.dumps(payload), 'https://serpapi.com/search.json')):
            links, reports = search_pages(self.product)
        self.assertEqual(len(links), 1)
        self.assertNotIn('price_cents', links[0])
        self.assertEqual(links[0]['url'], self.link['url'])
        self.assertEqual(reports[0]['status'], 'ok')

    def test_paid_plan_or_exhausted_quota_prevents_search(self):
        for account in [dict(account_status='Active', plan_monthly_price=25, plan_name='Starter', plan_searches_left=1000),
                        dict(account_status='Active', plan_monthly_price=0, plan_name='Free', plan_searches_left=0)]:
            with patch('discovery.read_public', return_value=(json.dumps(account),'https://serpapi.com/account.json')):
                with self.assertRaises(ValueError):
                    free_account('test-key')
        with patch('provider_settings.read_key', return_value='test-key'), \
             patch('provider_settings.free_account', side_effect=ValueError('Cota esgotada.')), \
             patch('discovery.read_public') as remote:
            links, reports = search_pages(self.product)
            remote.assert_not_called()
        self.assertEqual(links, [])
        self.assertEqual(reports[0]['status'], 'error')


class ProviderConfigurationTest(unittest.TestCase):
    def test_configured_key_is_never_returned_and_can_be_removed(self):
        if os.environ.get('DB_NAME') != 'preco_test':
            raise RuntimeError('Use the dedicated test database.')
        client = app.test_client()
        headers = {'X-Requested-With':'PrecoCerto'}
        key = 'test-only-key-' + 'x' * 40
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'search-api-key'
            with patch('provider_settings.key_path', return_value=target), patch('app.key_path', return_value=target), \
                 patch('app.free_account', return_value=dict(plan='Free',remaining=250)):
                result = client.put('/api/search-provider',json={'api_key':key},headers=headers)
                self.assertEqual(result.status_code,200)
                self.assertEqual(target.read_text(),key)
                self.assertNotIn(key,result.get_data(as_text=True))
                status = client.get('/api/search-provider')
                self.assertTrue(status.json['configured'])
                self.assertNotIn(key,status.get_data(as_text=True))
                self.assertEqual(client.delete('/api/search-provider',headers=headers).status_code,200)
                self.assertFalse(target.exists())
