"""Shopping advertisements are leads, never confirmed historical prices."""
import json
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode, urlparse

from collector import normalized, price_cents
from discovery import read_public, direct_product_url, inspect_page
from provider_settings import read_key, free_account


def api_search(key, **parameters):
    body, _ = read_public('https://serpapi.com/search.json?' + urlencode({**parameters, 'api_key': key}))
    data = json.loads(body)
    if data.get('error'):
        raise ValueError('O provedor não concluiu a consulta.')
    return data


def collect_shopping(product, preferences):
    key = read_key()
    if not key:
        return [], [dict(store='Google Shopping',status='unconfigured',message='Ative a busca ampliada nas preferências.')]
    try:
        account = free_account(key)
        # Discover the model broadly; sizes in search titles are often absent.
        # The exact variant is checked on each merchant page before entering history.
        data = api_search(key,engine='google_shopping',q=f"{product['brand']} {product['model']}",gl='br',hl='pt-br')
        required = set(normalized(product['brand']) + normalized(product['model']))
        candidates = []
        for result in data.get('shopping_results', []):
            if not required <= set(normalized(result.get('title',''))):
                continue
            amount = price_cents(result.get('extracted_price'))
            google_url = result.get('product_link','')
            parsed = urlparse(google_url)
            if not amount or parsed.scheme != 'https' or parsed.hostname not in ('www.google.com','google.com'):
                continue
            candidates.append(dict(title=str(result['title'])[:500],store=str(result.get('source','Loja'))[:160],
                                   indexed_price_cents=amount,google_url=google_url,
                                   token=result.get('immersive_product_page_token'),status='unconfirmed'))
        size_tokens = set(normalized(product.get('desired_size','')))
        candidates.sort(key=lambda item:(item['indexed_price_cents'],
                                         0 if size_tokens and size_tokens <= set(normalized(item['title'])) else 1))
        # Resolve at most three distinct sellers, prioritizing lower advertised prices.
        selected, seen = [], set()
        budget = min(3, max(0, account['remaining'] - 2))
        for item in candidates:
            if item['store'].casefold() not in seen and item['token'] and len(selected) < budget:
                selected.append(item)
                seen.add(item['store'].casefold())

        def resolve(item):
            try:
                detail = api_search(key,engine='google_immersive_product',page_token=item['token'],more_stores='true')
                stores = detail.get('product_results',{}).get('stores',[])
                return item, [dict(title=store.get('title') or item['title'],url=store.get('link',''),store=store.get('name',''),
                                   indexed_price_cents=price_cents(store.get('extracted_price')))
                              for store in stores if direct_product_url(store.get('link',''))][:13]
            except Exception:
                return item, []
        with ThreadPoolExecutor(max_workers=3) as executor:
            resolved = list(executor.map(resolve,selected))
        links = {}
        for item, pages in resolved:
            wanted_store = set(normalized(item['store'])) - {'oficial','com','br'}
            same_store = next((page for page in pages if wanted_store and wanted_store <= set(normalized(page['store']))),None)
            if same_store:
                item['merchant_url'] = same_store['url']
            for page in pages:
                links.setdefault(page['url'],page)
        pages = list(links.values())[:20]
        def verify(page):
            offer, record = inspect_page(page,product,preferences)
            if record and not offer:
                record['indexed_price_cents'] = page['indexed_price_cents']
            return offer, record
        with ThreadPoolExecutor(max_workers=5) as executor:
            checked = list(executor.map(verify,pages))
        safe_originals = {page['url'] for page,(_,record) in zip(pages,checked) if record}
        for item in candidates:
            item.pop('token',None)
            if item.get('merchant_url') not in safe_originals:
                item.pop('merchant_url',None)
        return [offer for offer,_ in checked if offer], [dict(
            store='Google Shopping',status='ok' if candidates else 'empty',
            count=len(candidates),message=f'{len(candidates)} anúncios do modelo encontrados; preços, tamanho e pagamento precisam de confirmação na loja.',
            shopping_results=candidates[:30],links=[record for _,record in checked if record])]
    except Exception:
        return [], [dict(store='Google Shopping',status='error',message='Não foi possível concluir a consulta ao Shopping. Nenhum preço de anúncio foi gravado no histórico.')]
