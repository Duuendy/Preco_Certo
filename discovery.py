"""Discover public product pages; only explicit in-stock BRL offers enter history."""
import http.client
import ipaddress
import json
import re
import socket
import ssl
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from urllib.parse import quote, urlencode, urljoin, urlparse, urlunparse

MAX_BYTES = 3 * 1024 * 1024
EXCLUDED = {'bing.com', 'google.com', 'duckduckgo.com', 'youtube.com', 'reddit.com',
            'buscape.com.br', 'zoom.com.br', 'pelando.com.br', 'promobit.com.br', 'cupoom.com.br'}


class UnsafeURL(ValueError):
    pass


def public_addresses(host):
    addresses = list(dict.fromkeys(row[4][0] for row in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)))
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise UnsafeURL('Endereço de rede não permitido.')
    return addresses


def read_public(url):
    # Connect to the validated IP while preserving TLS verification and Host.
    # Every redirect is checked again, preventing DNS rebinding and private-network requests.
    for _ in range(4):
        parsed = urlparse(url)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None, 443):
            raise UnsafeURL('Use um endereço HTTPS público.')
        address = public_addresses(parsed.hostname)[0]
        context = ssl.create_default_context()
        conn = http.client.HTTPSConnection(parsed.hostname, timeout=8, context=context)
        sock = socket.create_connection((address, 443), timeout=8)
        try:
            conn.sock = context.wrap_socket(sock, server_hostname=parsed.hostname)
            path = urlunparse(('', '', parsed.path or '/', parsed.params, parsed.query, ''))
            conn.request('GET', path, headers={'Accept': 'text/html,application/xml,application/json',
                                             'Accept-Encoding': 'identity', 'User-Agent': 'PrecoCerto/1.2'})
            response = conn.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                url = urljoin(url, response.getheader('Location', ''))
                continue
            if response.status != 200:
                raise ValueError('Consulta pública indisponível.')
            raw = response.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise ValueError('Página excedeu o limite de leitura.')
            return raw.decode('utf-8', 'replace'), url
        finally:
            conn.close()
            sock.close()
    raise ValueError('Redirecionamentos em excesso.')


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.scripts = []
        self.links = []
        self.active = False
        self.buffer = []
        self.link = None
        self.link_text = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'script' and attrs.get('type', '').lower() == 'application/ld+json':
            self.active, self.buffer = True, []
        if tag == 'a' and 'result__a' in attrs.get('class', '').split():
            self.link, self.link_text = attrs.get('href'), []

    def handle_data(self, data):
        if self.active:
            self.buffer.append(data)
        if self.link is not None:
            self.link_text.append(data)

    def handle_endtag(self, tag):
        if tag == 'script' and self.active:
            try:
                self.scripts.append(json.loads(''.join(self.buffer)))
            except (ValueError, TypeError):
                pass
            self.active = False
        if tag == 'a' and self.link is not None:
            self.links.append({'url': self.link, 'title': ''.join(self.link_text)})
            self.link = None


def product_nodes(value):
    if isinstance(value, list):
        for item in value:
            yield from product_nodes(item)
    elif isinstance(value, dict):
        types = value.get('@type', [])
        types = [types] if isinstance(types, str) else types
        if 'Product' in types:
            yield value
        for key in ('@graph', 'mainEntity', 'itemListElement', 'item'):
            if key in value:
                yield from product_nodes(value[key])


def direct_product_url(url):
    parsed = urlparse(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
        return False
    host = parsed.hostname.removeprefix('www.')
    if any(host == domain or host.endswith('.' + domain) for domain in EXCLUDED):
        return False
    path = parsed.path.lower()
    if len(path.strip('/')) < 8 or re.search(r'/(busca|search|nav|categoria|category|loja|blog|lista|tag)(/|$)', path):
        return False
    if any(key in parsed.query.lower() for key in ('q=', 'query=', 'search=')):
        return False
    return True


def search_pages(product, allow_api=True):
    # Two independent public indexes; bounded requests, no challenge bypass or proxy rotation.
    from collector import search_query
    query = search_query(product)
    urls = [
        ('Bing', 'https://www.bing.com/search?' + urlencode({'format': 'rss', 'q': query}, quote_via=quote)),
        ('DuckDuckGo', 'https://html.duckduckgo.com/html/?' + urlencode({'q': query, 'kl': 'br-pt'}, quote_via=quote)),
    ]
    from provider_settings import read_key, free_account
    api_key = read_key() if allow_api else ''
    if api_key:
        try:
            free_account(api_key)
        except ValueError as error:
            return [], [dict(store='Busca ampliada', status='error', message=str(error))]
        # One web-search request per collection; use merchant links, never snippet prices.
        urls = [('Google via SerpApi', 'https://serpapi.com/search.json?' + urlencode(
            {'engine': 'google', 'q': query, 'gl': 'br', 'hl': 'pt-br', 'num': 20, 'api_key': api_key}, quote_via=quote))]
    from collector import normalized
    required = set(normalized(product['model']) + normalized(product['brand']))

    def fetch(engine):
        name, url = engine
        try:
            body, _ = read_public(url)
            if name == 'Google via SerpApi':
                payload = json.loads(body)
                if payload.get('error'):
                    raise ValueError('API de busca indisponível.')
                links = [dict(url=item.get('link', ''), title=item.get('title', '')) for item in payload.get('organic_results', [])]
            elif name == 'Bing':
                root = ET.fromstring(body)
                links = [dict(url=item.findtext('link', ''), title=item.findtext('title', '')) for item in root.findall('.//item')]
            else:
                parser = PageParser()
                parser.feed(body)
                links = parser.links
                from urllib.parse import parse_qs, unquote
                for link in links:
                    target = parse_qs(urlparse(link['url']).query).get('uddg')
                    if target:
                        link['url'] = unquote(target[0])
            candidates = [link for link in links if direct_product_url(link['url']) and required <= set(normalized(link['title']))]
            return candidates, dict(store=f'Busca web · {name}', status='ok' if candidates else 'empty',
                                    message=f'{len(candidates)} páginas de possíveis produtos encontradas.')
        except Exception:
            return [], dict(store=f'Busca web · {name}', status='error', message='O buscador não respondeu ou bloqueou a consulta pública.')
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(fetch, urls))
    unique = {link['url']: link for links, _ in results for link in links}
    if not unique and not api_key and allow_api:
        results.append(([], dict(store='Ampliar descoberta', status='unconfigured',
                                 message='Ative a busca ampliada em Preferências. É necessária uma conta gratuita no provedor.')))
    return list(unique.values())[:20], [report for _, report in results]


def inspect_page(link, product, preferences):
    from collector import normalized, price_cents, variant_matches
    record = dict(title=link['title'][:500], url=link['url'], store=urlparse(link['url']).hostname,
                  status='unconfirmed', message='Preço e estoque não confirmados. Confira na loja.')
    try:
        body, final_url = read_public(link['url'])
        if not direct_product_url(final_url):
            return None, None
        record['url'] = final_url
        record['store'] = urlparse(final_url).hostname
        parser = PageParser()
        parser.feed(body)
        required = set(normalized(product['brand']) + normalized(product['model']) + normalized(product.get('requirements', '')))
        for node in product_nodes(parser.scripts):
            if not set(normalized(product['model'])) <= set(normalized(node.get('name', ''))):
                continue
            size = node.get('size', '')
            if isinstance(size, dict):
                size = size.get('name', '')
            if not variant_matches(product, size, node.get('color', '')):
                continue
            brand = node.get('brand', '')
            brand = brand.get('name', '') if isinstance(brand, dict) else brand
            terms = set(normalized(str(node.get('name', '')) + ' ' + str(brand) + ' ' + str(node.get('description', ''))))
            if not required <= terms:
                continue
            record['title'] = str(node.get('name', link['title']))[:500]
            offers = node.get('offers', [])
            offers = [offers] if isinstance(offers, dict) else offers
            for offer in offers:
                if not isinstance(offer, dict) or offer.get('@type') != 'Offer':
                    continue
                amount = price_cents(offer.get('price') or (offer.get('priceSpecification') or {}).get('price'))
                if not amount or offer.get('priceCurrency') != 'BRL' or str(offer.get('availability', '')).rsplit('/', 1)[-1] != 'InStock':
                    continue
                condition = str(offer.get('itemCondition') or node.get('itemCondition', '')).rsplit('/', 1)[-1]
                if condition != 'NewCondition' or product['desired_condition'] not in ('Novo', 'Qualquer'):
                    continue
                offer_url = urljoin(final_url, str(offer.get('url') or final_url))
                if not direct_product_url(offer_url) or urlparse(offer_url).hostname != urlparse(final_url).hostname:
                    continue
                # No payment/origin claims are inferred from missing structured data.
                record.update(status='verified', url=offer_url, price_cents=amount, message='Preço e estoque informados na página. Confirme pagamento e variação.')
                result = dict(store=record['store'][:160], url=offer_url, title=record['title'], price_cents=amount,
                              payment='Confirmar na loja', payment_known=False, imported=False, imported_known=False, condition='Novo')
                return result, record
        return None, record
    except UnsafeURL:
        return None, None
    except Exception:
        record['message'] = 'Página encontrada na busca; a loja não permitiu confirmar preço e estoque.'
        return None, record


def collect_web(product, preferences, allow_api=True):
    links, reports = search_pages(product,allow_api)
    with ThreadPoolExecutor(max_workers=5) as executor:
        results = list(executor.map(lambda link: inspect_page(link, product, preferences), links))
    offers = [offer for offer, _ in results if offer]
    pages = [page for _, page in results if page]
    reports.append(dict(store='Outras lojas na web', status='ok' if offers else 'unconfirmed' if pages else 'empty',
                        count=len(offers), message='Cobertura dos resultados públicos disponíveis; não representa todas as lojas da internet.', links=pages))
    return offers, reports
