import unittest
from unittest.mock import patch

from shopping import collect_shopping


class ShoppingCoverageTest(unittest.TestCase):
    def setUp(self):
        self.product = dict(brand='Mizuno',model='Neo Aura Knit',desired_size='40')
        self.ad = dict(title='Mizuno Neo Aura Knit 40',source='Loja A',extracted_price=427.49,
                       product_link='https://www.google.com/search?product=123',immersive_product_page_token='page-token')
        self.url = 'https://loja.example/produto/neo-aura-knit'

    def execute(self, verify_result, stores=None, ads=None):
        stores = stores if stores is not None else [dict(name='Loja A',link=self.url,title=self.ad['title'],extracted_price=427.49)]
        def response(key, **parameters):
            if parameters['engine'] == 'google_shopping':
                return dict(shopping_results=ads if ads is not None else [self.ad])
            return dict(product_results=dict(stores=stores))
        with patch('shopping.read_key',return_value='test-key'),patch('shopping.free_account',return_value=dict(remaining=100)), \
             patch('shopping.api_search',side_effect=response),patch('shopping.inspect_page',return_value=verify_result):
            return collect_shopping(self.product,{})

    def test_lower_ad_is_visible_but_never_becomes_historical_price_without_confirmation(self):
        offers,reports = self.execute((None,dict(url=self.url,status='unconfirmed')))
        self.assertEqual(offers,[])
        ad = reports[0]['shopping_results'][0]
        self.assertEqual(ad['indexed_price_cents'],42749)
        self.assertEqual(ad['merchant_url'],self.url)
        self.assertNotIn('token',ad)

    def test_actual_merchant_price_overrides_advertised_price_for_history(self):
        offer = dict(url=self.url,price_cents=49999)
        offers,reports = self.execute((offer,dict(url=self.url,status='verified')))
        self.assertEqual(offers[0]['price_cents'],49999)
        self.assertEqual(reports[0]['shopping_results'][0]['indexed_price_cents'],42749)

    def test_other_models_are_filtered_and_other_seller_is_not_mislabeled(self):
        wrong = {**self.ad,'title':'Mizuno Neo Zen'}
        offers,reports = self.execute((None,dict(url=self.url,status='unconfirmed')),
                                     stores=[dict(name='Loja B',link=self.url,extracted_price=500)],ads=[self.ad,wrong])
        self.assertEqual(len(reports[0]['shopping_results']),1)
        self.assertNotIn('merchant_url',reports[0]['shopping_results'][0])

    def test_unsafe_redirect_does_not_publish_merchant_link(self):
        offers,reports = self.execute((None,None))
        self.assertNotIn('merchant_url',reports[0]['shopping_results'][0])

    def test_resolutions_are_bounded_by_three_stores(self):
        ads = [{**self.ad,'source':f'Loja {i}','immersive_product_page_token':f'token{i}'} for i in range(10)]
        with patch('shopping.read_key',return_value='test-key'),patch('shopping.free_account',return_value=dict(remaining=100)), \
             patch('shopping.api_search',side_effect=lambda key,**p: dict(shopping_results=ads) if p['engine']=='google_shopping' else dict(product_results={})) as api:
            collect_shopping(self.product,{})
        self.assertEqual(api.call_count,4)

    def test_missing_key_does_not_consume_quota(self):
        with patch('shopping.read_key',return_value=''),patch('shopping.api_search') as api:
            offers,reports=collect_shopping(self.product,{})
        api.assert_not_called()
        self.assertEqual(reports[0]['status'],'unconfigured')
