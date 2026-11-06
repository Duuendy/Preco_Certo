"""Read-only collection from a bounded list of public Brazilian store catalogs."""
import json
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode, urlparse, parse_qsl, urlunparse, quote
from urllib.request import Request, urlopen

# These URLs are fixed by the application, never supplied by visitors.
STORES = [
    ('Mizuno', 'https://www.mizuno.com.br', 'mizuno'),
    ('Decathlon', 'https://www.decathlon.com.br', None),
    ('Carrefour', 'https://www.carrefour.com.br', None),
]


def normalized(value):
    plain = unicodedata.normalize('NFKD', str(value)).encode('ascii', 'ignore').decode().lower()
    return re.findall(r'[a-z0-9]+', plain)


def search_query(product):
    query = f'"{product["model"]}" {product["brand"]}'
    if product.get('desired_size'):
        query += ' ' + product['desired_size']
    colors = [color.strip() for color in product.get('preferred_colors', '').split(',') if color.strip()]
    if colors:
        query += ' (' + ' OR '.join(f'"{color}"' for color in colors) + ')'
    return query + ' comprar'


def variant_matches(product, size, color):
    wanted_size = set(normalized(product.get('desired_size', '')))
    if wanted_size and not wanted_size <= set(normalized(size)):
        return False
    colors = [set(normalized(c)) - {'e', 'ou'} for c in product.get('preferred_colors', '').split(',') if c.strip()]
    available = set(normalized(color)) - {'e', 'ou'}
    return not colors or any(wanted and wanted <= available for wanted in colors)


def matches(product, candidate, item):
    # Every model token must match. Never substitute Neo Zen for Neo Aura Knit.
    wanted = set(normalized(product['model']))
    title = set(normalized(candidate.get('productName', '')))
    brand = set(normalized(product['brand']))
    if not wanted or not wanted <= title or not brand <= set(normalized(candidate.get('brand', ''))):
        return False
    size = item.get('Tamanho') or item.get('nameComplete', '')
    color = item.get('Cor') or item.get('nameComplete', '')
    if not variant_matches(product, size, color):
        return False
    detail = ' '.join(str(v) for v in [candidate.get('productName', ''), candidate.get('description', ''),
                                      candidate.get('categories', []), candidate.get('allSpecifications', []),
                                      {k: candidate.get(k) for k in candidate.get('allSpecifications', [])},
                                      item.get('nameComplete', ''),
                                      {k: item.get(k) for k in item.get('variations', [])}])
    required = set(normalized(product.get('requirements', '')))
    # Conservative matching: an unconfirmed required specification is excluded.
    if required and not required <= set(normalized(detail)):
        return False
    if set(normalized(candidate.get('productName', ''))) & {'usado', 'usada', 'recondicionado', 'recondicionada'}:
        return False
    return True


def price_cents(value):
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount <= 0 or amount > 100_000_000:
            return None
        return int((amount * 100).quantize(Decimal('1')))
    except (InvalidOperation, ValueError, TypeError):
        return None


def fetch_catalog(base, query):
    params = urlencode({'ft': query, '_from': 0, '_to': 49}, quote_via=quote)
    request = Request(f'{base}/api/catalog_system/pub/products/search?{params}',
                      headers={'Accept': 'application/json', 'User-Agent': 'PrecoCerto/1.1 (personal price research)'})
    with urlopen(request, timeout=12) as response:
        raw = response.read(12 * 1024 * 1024 + 1)
    if len(raw) > 12 * 1024 * 1024:
        raise ValueError('Catálogo excedeu o limite de leitura.')
    data = json.loads(raw)
    if not isinstance(data, list):
        raise ValueError('Formato do catálogo indisponível.')
    return data


def collect_store(store, product, preferences):
    name, base, brand_only = store
    if brand_only and brand_only not in normalized(product['brand']):
        return [], {'store': name, 'status': 'skipped', 'message': 'Loja de outra marca.'}
    if product['desired_condition'] not in ('Novo', 'Qualquer'):
        return [], {'store': name, 'status': 'skipped', 'message': 'Este catálogo consulta apenas produtos novos.'}
    try:
        catalog = fetch_catalog(base, f"{product['brand']} {product['model']}")
        offers = []
        for candidate in catalog:
            origin = ' '.join(normalized(candidate.get('Origem', '')))
            imported_known = bool(re.search(r'\b(nacional|importado|importada)\b', origin))
            imported = bool(re.search(r'\b(importado|importada)\b', origin))
            if imported and not preferences['imported']:
                continue
            link = candidate.get('link', '')
            parsed = urlparse(link)
            if parsed.scheme != 'https' or parsed.hostname != urlparse(base).hostname:
                continue
            for item in candidate.get('items', []):
                if not matches(product, candidate, item):
                    continue
                for seller in item.get('sellers', []):
                    commercial = seller.get('commertialOffer', {})
                    price = price_cents(commercial.get('Price'))
                    if not price or commercial.get('AvailableQuantity', 0) <= 0:
                        continue
                    # Keep only payment types explicitly associated with this catalog price.
                    payments = set()
                    for plan in commercial.get('Installments', []):
                        if plan.get('NumberOfInstallments') != 1 or price_cents(plan.get('TotalValuePlusInterestRate')) != price:
                            continue
                        group = plan.get('PaymentSystemGroupName', '')
                        label = {'creditCardPaymentGroup': 'Cartão de crédito', 'instantPaymentPaymentGroup': 'Pix',
                                 'bankInvoicePaymentGroup': 'Boleto', 'debitCardPaymentGroup': 'Débito'}.get(group)
                        if label:
                            payments.add(label)
                    accepted = sorted(payments & set(preferences['payments']))
                    if not accepted:
                        continue
                    sku = str(item.get('itemId', ''))
                    query = dict(parse_qsl(parsed.query))
                    query['idsku'] = sku
                    offer_link = urlunparse(parsed._replace(query=urlencode(query)))
                    offers.append(dict(store=f"{name} · {seller.get('sellerName', name)}"[:160], url=offer_link,
                                       title=str(item.get('nameComplete') or candidate['productName'])[:500],
                                       price_cents=price, payment=', '.join(accepted), imported=imported,
                                       imported_known=imported_known, condition='Novo',
                                       _shipping=(base,sku,str(seller.get('sellerId','')),preferences.get('cep',''),price)))
        offers.sort(key=lambda offer: (offer['price_cents'], offer['url']))
        # One observation for every available SKU/seller; preserve color and size in its title.
        unique = {(o['url'], o['store']): o for o in offers}
        result = list(unique.values())[:150]
        from shipping import quote_shipping
        if preferences.get('cep'):
            def quote(offer):
                estimate=quote_shipping(*offer['_shipping'])
                if estimate:
                    offer.update(estimate)
            with ThreadPoolExecutor(max_workers=3) as executor:
                list(executor.map(quote,result[:10]))
        for offer in result:
            offer.pop('_shipping',None)
        capped = len(catalog) >= 50 or len(unique) > 150
        return result, {'store': name, 'status': 'ok' if result else 'empty', 'count': len(result),
                        'message': 'Consulta limitada aos primeiros resultados.' if capped else 'Catálogo público consultado.'}
    except Exception:
        # Do not expose remote response bodies or infrastructure details to visitors.
        return [], {'store': name, 'status': 'error', 'message': 'A loja não respondeu ou bloqueou a consulta pública.'}


def collect(product, preferences, allow_api=True):
    from discovery import collect_web
    from shopping import collect_shopping
    with ThreadPoolExecutor(max_workers=5) as executor:
        web = executor.submit(collect_web, product, preferences, allow_api)
        shopping = executor.submit(collect_shopping, product, preferences) if allow_api else None
        known = executor.submit(collect_known, product, preferences)
        results = list(executor.map(lambda store: collect_store(store, product, preferences), STORES))
        web_offers, web_reports = web.result()
        shopping_offers, shopping_reports = shopping.result() if shopping else ([],[dict(store='SerpApi',status='skipped',message='Consulta sem API: fora da janela dos dias 15 a 20 ou orçamento reservado esgotado.')])
        known_offers,known_reports=known.result()
    offers = [offer for result, _ in results for offer in result] + web_offers + shopping_offers + known_offers
    unique = {}
    for offer in offers:
        key = (offer['url'], offer['store'])
        previous = unique.get(key)
        if previous is None or (offer.get('shipping_known', False) and not previous.get('shipping_known', False)):
            unique[key] = offer
    return sorted(unique.values(), key=lambda offer: offer['price_cents']), [report for _, report in results] + web_reports + shopping_reports + known_reports


def collect_known(product,preferences):
    from discovery import inspect_page
    links=product.get('_known_links',[])[:20]
    with ThreadPoolExecutor(max_workers=3) as executor:
        results=list(executor.map(lambda link:inspect_page(link,product,preferences),links))
    offers=[offer for offer,_ in results if offer]
    return offers,[dict(store='Links já encontrados',status='ok' if offers else 'empty',count=len(offers),
                         message=f'{len(links)} páginas consultadas diretamente, sem SerpApi.',links=[page for _,page in results if page])]
