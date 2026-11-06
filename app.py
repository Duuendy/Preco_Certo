import io
import os
import re
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlparse

import psycopg
from psycopg.rows import dict_row
from flask import Flask, jsonify, request, send_file, send_from_directory
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from PIL import Image, ImageOps, UnidentifiedImageError
from werkzeug.exceptions import HTTPException
from provider_settings import key_path, read_key, save_key, free_account
from collector import search_query
import monitoring_schedule as schedule

app = Flask(__name__, static_url_path='/static')
app.config['MAX_CONTENT_LENGTH'] = 20 * 1024 * 1024
UPLOADS = Path(os.environ.get('UPLOAD_DIR', './data/photos'))
UPLOADS.mkdir(parents=True, exist_ok=True)
ELIGIBLE_TOTAL = '''o.shipping_known AND o.payment_known AND o.shipping_cep=%(cep)s AND %(cep)s<>''
    AND regexp_split_to_array(o.payment, ', ') && %(payments)s
    AND (%(condition)s='Qualquer' OR o.condition=%(condition)s)
    AND (%(imported)s OR (o.imported_known AND NOT o.imported))'''


def connect():
    return psycopg.connect(host=os.environ.get('DB_HOST', 'localhost'),
                          dbname=os.environ.get('DB_NAME', 'preco'), user='preco',
                          password=os.environ['DB_PASSWORD'], row_factory=dict_row)


def initialize():
    with connect() as db:
        # Serialize startup across Gunicorn workers.
        db.execute('SELECT pg_advisory_xact_lock(781203)')
        db.execute('''
            CREATE TABLE IF NOT EXISTS products (
                id SERIAL PRIMARY KEY, name TEXT NOT NULL, brand TEXT NOT NULL,
                model TEXT NOT NULL, requirements TEXT NOT NULL DEFAULT '',
                desired_condition TEXT NOT NULL DEFAULT 'Novo', target_cents BIGINT NOT NULL CHECK(target_cents >= 0),
                category TEXT NOT NULL DEFAULT 'Outros', archived BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now());
            CREATE TABLE IF NOT EXISTS photos (
                id SERIAL PRIMARY KEY, product_id INTEGER NOT NULL REFERENCES products(id),
                filename TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS searches (
                id SERIAL PRIMARY KEY, product_id INTEGER NOT NULL REFERENCES products(id),
                verified_at TIMESTAMPTZ NOT NULL, notes TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT 'manual', created_at TIMESTAMPTZ NOT NULL DEFAULT now());
            CREATE TABLE IF NOT EXISTS offers (
                id SERIAL PRIMARY KEY, search_id INTEGER NOT NULL REFERENCES searches(id),
                store TEXT NOT NULL, url TEXT NOT NULL, price_cents BIGINT NOT NULL CHECK(price_cents >= 0),
                shipping_cents BIGINT NOT NULL CHECK(shipping_cents >= 0),
                discount_cents BIGINT NOT NULL CHECK(discount_cents >= 0),
                final_cents BIGINT NOT NULL CHECK(final_cents >= 0),
                payment TEXT NOT NULL, condition TEXT NOT NULL, imported BOOLEAN NOT NULL DEFAULT FALSE);
            CREATE INDEX IF NOT EXISTS searches_product_date ON searches(product_id, verified_at);
            CREATE INDEX IF NOT EXISTS offers_search ON offers(search_id);
            CREATE TABLE IF NOT EXISTS preferences (
                id INTEGER PRIMARY KEY CHECK(id = 1), cep TEXT NOT NULL DEFAULT '',
                payments TEXT[] NOT NULL DEFAULT ARRAY['Pix','Cartão de crédito'],
                imported BOOLEAN NOT NULL DEFAULT FALSE, used BOOLEAN NOT NULL DEFAULT FALSE);
            INSERT INTO preferences(id) VALUES(1) ON CONFLICT DO NOTHING;
            ALTER TABLE products ADD COLUMN IF NOT EXISTS revision INTEGER NOT NULL DEFAULT 1;
            ALTER TABLE products ADD COLUMN IF NOT EXISTS desired_size TEXT NOT NULL DEFAULT '';
            ALTER TABLE products ADD COLUMN IF NOT EXISTS preferred_colors TEXT NOT NULL DEFAULT '';
            CREATE TABLE IF NOT EXISTS collection_jobs (
                id SERIAL PRIMARY KEY, product_id INTEGER NOT NULL REFERENCES products(id),
                product_revision INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
                message TEXT NOT NULL DEFAULT 'Aguardando pesquisa automática…', report JSONB NOT NULL DEFAULT '[]',
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(), started_at TIMESTAMPTZ, finished_at TIMESTAMPTZ);
            CREATE UNIQUE INDEX IF NOT EXISTS collection_active_product ON collection_jobs(product_id,product_revision)
                WHERE status IN ('queued','running');
            ALTER TABLE searches ADD COLUMN IF NOT EXISTS collection_job_id INTEGER UNIQUE REFERENCES collection_jobs(id);
            ALTER TABLE searches ADD COLUMN IF NOT EXISTS product_revision INTEGER NOT NULL DEFAULT 1;
            ALTER TABLE offers ADD COLUMN IF NOT EXISTS title TEXT NOT NULL DEFAULT '';
            ALTER TABLE offers ADD COLUMN IF NOT EXISTS shipping_known BOOLEAN NOT NULL DEFAULT TRUE;
            ALTER TABLE offers ADD COLUMN IF NOT EXISTS imported_known BOOLEAN NOT NULL DEFAULT TRUE;
            ALTER TABLE offers ADD COLUMN IF NOT EXISTS payment_known BOOLEAN NOT NULL DEFAULT TRUE;
            ALTER TABLE offers ALTER COLUMN shipping_cents DROP NOT NULL;
            ALTER TABLE offers ALTER COLUMN final_cents DROP NOT NULL;
            ALTER TABLE collection_jobs ADD COLUMN IF NOT EXISTS trigger TEXT NOT NULL DEFAULT 'manual';
            ALTER TABLE collection_jobs ADD COLUMN IF NOT EXISTS scheduled_for DATE;
            CREATE UNIQUE INDEX IF NOT EXISTS collection_daily_product ON collection_jobs(product_id,scheduled_for);
            CREATE TABLE IF NOT EXISTS api_reservations (
                job_id INTEGER PRIMARY KEY REFERENCES collection_jobs(id),month DATE NOT NULL,units INTEGER NOT NULL);
            ALTER TABLE offers ADD COLUMN IF NOT EXISTS shipping_cep TEXT NOT NULL DEFAULT '';
            ALTER TABLE offers ADD COLUMN IF NOT EXISTS delivery_estimate TEXT NOT NULL DEFAULT '';
            ALTER TABLE offers ADD COLUMN IF NOT EXISTS tax_cents BIGINT NOT NULL DEFAULT 0;
            CREATE TABLE IF NOT EXISTS notifications (
                id SERIAL PRIMARY KEY,product_id INTEGER NOT NULL REFERENCES products(id),
                search_id INTEGER NOT NULL REFERENCES searches(id),offer_id INTEGER NOT NULL REFERENCES offers(id),
                fingerprint TEXT NOT NULL UNIQUE,body TEXT NOT NULL,url TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                sent_at TIMESTAMPTZ,error TEXT NOT NULL DEFAULT '');
        ''')


def fail(message, code=400):
    response = jsonify(error=message)
    response.status_code = code
    return response


def text_field(data, key, required=False, limit=500):
    value = data.get(key, '')
    if not isinstance(value, str) or len(value.strip()) > limit:
        raise ValueError(f'Campo inválido: {key}.')
    value = value.strip()
    if required and not value:
        raise ValueError(f'Preencha o campo {key}.')
    return value


def cents(value):
    if isinstance(value, bool):
        raise ValueError('Informe um valor monetário válido.')
    try:
        number = Decimal(str(value))
        if not number.is_finite() or number < 0 or number > 100_000_000 or number != number.quantize(Decimal('.01')):
            raise ValueError('Use valores positivos com até duas casas decimais.')
        return int(number * 100)
    except (InvalidOperation, TypeError):
        raise ValueError('Informe um valor monetário válido.')


def flag(data, key):
    value = data.get(key, False)
    if not isinstance(value, bool):
        raise ValueError(f'Opção inválida: {key}.')
    return value


def condition(value):
    if value not in ('Novo', 'Usado', 'Recondicionado', 'Qualquer'):
        raise ValueError('Condição inválida.')
    return value


def json_body():
    data = request.get_json()
    if not isinstance(data, dict):
        raise ValueError('Envie um objeto JSON.')
    return data


@app.errorhandler(ValueError)
def validation_error(error):
    return fail(str(error))


@app.errorhandler(HTTPException)
def http_error(error):
    return fail('Arquivo muito grande. Limite total: 20 MB.' if error.code == 413 else error.description, error.code)


@app.before_request
def guard_writes():
    if request.method in ('POST', 'PUT', 'PATCH', 'DELETE') and request.headers.get('X-Requested-With') != 'PrecoCerto':
        return fail('Requisição não autorizada.', 403)


@app.after_request
def security_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['Content-Security-Policy'] = "default-src 'self'; img-src 'self' blob:; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    if request.path.startswith('/api'):
        response.headers['Cache-Control'] = 'no-store'
    return response


@app.get('/')
def index():
    return app.send_static_file('index.html')


@app.get('/api/health')
def health():
    with connect() as db:
        db.execute('SELECT 1')
    return jsonify(status='ok')


def price_summary(history):
    if not history:
        return dict(current_cents=None, historical_low_cents=None, previous_low_cents=None,
                    difference_cents=None, difference_percent=None, verdict='unavailable')
    current = history[-1]['best_price_cents']
    previous = min((h['best_price_cents'] for h in history[:-1]), default=None)
    difference = current - previous if previous is not None else None
    return dict(current_cents=current, historical_low_cents=min(h['best_price_cents'] for h in history),
                previous_low_cents=previous, difference_cents=difference,
                difference_percent=round(difference / previous * 100, 2) if previous else None,
                verdict='first' if previous is None else 'lower' if difference < 0 else 'higher' if difference > 0 else 'equal')


def enqueue_collection(db, product, trigger='manual'):
    if product['archived']:
        raise ValueError('Reative o produto antes de pesquisar.')
    active = db.execute("""SELECT id,status FROM collection_jobs WHERE product_id=%s AND product_revision=%s
        AND (status IN ('queued','running') OR created_at > now() - interval '60 seconds') ORDER BY id DESC LIMIT 1""",
        (product['id'], product['revision'])).fetchone()
    if active:
        if active['status'] == 'deferred' and trigger == 'manual':
            db.execute("UPDATE collection_jobs SET status='queued',trigger='manual',message='Consulta manual em fontes gratuitas.' WHERE id=%s",(active['id'],))
            return dict(id=active['id'],status='queued')
        return active
    deferred = trigger != 'manual' and schedule.phase() == 'paused'
    return db.execute('''INSERT INTO collection_jobs(product_id,product_revision,trigger,status,message)
        VALUES(%s,%s,%s,%s,%s) RETURNING id,status''',
        (product['id'],product['revision'],trigger,'deferred' if deferred else 'queued',
         'Monitoramento pausado do dia 1 ao 14. Retomada automática a partir do dia 15.' if deferred else 'Aguardando pesquisa…')).fetchone()


@app.post('/api/products/<int:product_id>/collect')
def request_collection(product_id):
    with connect() as db:
        product = db.execute('SELECT * FROM products WHERE id=%s FOR UPDATE', (product_id,)).fetchone()
        if not product:
            return fail('Produto não encontrado.', 404)
        job = enqueue_collection(db, product)
    return jsonify(job), 202


@app.get('/api/products')
def products():
    with connect() as db:
        rows = db.execute('SELECT * FROM products ORDER BY created_at DESC, id DESC').fetchall()
        prefs=db.execute('SELECT * FROM preferences WHERE id=1').fetchone()
        for product in rows:
            context=dict(id=product['id'],revision=product['revision'],cep=prefs['cep'],payments=prefs['payments'],
                         condition=product['desired_condition'],imported=prefs['imported'])
            product['search_query'] = search_query(product)
            product['photos'] = db.execute('SELECT id, filename FROM photos WHERE product_id=%s ORDER BY id', (product['id'],)).fetchall()
            product['history'] = db.execute(f'''SELECT s.id, s.verified_at, s.notes, s.source,
                min(o.final_cents) AS best_cents, min(o.price_cents-o.discount_cents) AS best_price_cents,
                min(o.final_cents) FILTER (WHERE {ELIGIBLE_TOTAL}) AS best_total_cents,
                count(o.id)::int AS offer_count
                FROM searches s JOIN offers o ON o.search_id=s.id WHERE s.product_id=%(id)s AND s.product_revision=%(revision)s
                GROUP BY s.id ORDER BY s.verified_at, s.id''', context).fetchall()
            product['price_summary'] = price_summary(product['history'])
            product['best_offer'] = db.execute(f'''SELECT o.store,o.url,o.title,o.payment,o.payment_known,o.final_cents,o.shipping_cents,o.shipping_known,
                CASE WHEN {ELIGIBLE_TOTAL} THEN o.final_cents END AS confirmed_final_cents,
                o.price_cents-o.discount_cents AS amount_cents,s.verified_at FROM offers o
                JOIN searches s ON s.id=o.search_id WHERE s.id=(SELECT id FROM searches
                WHERE product_id=%(id)s AND product_revision=%(revision)s ORDER BY verified_at DESC,id DESC LIMIT 1)
                ORDER BY confirmed_final_cents NULLS LAST,o.price_cents-o.discount_cents,o.id LIMIT 1''',context).fetchone()
            product['collection'] = db.execute('''SELECT id,status,message,report,created_at,finished_at FROM collection_jobs
                WHERE product_id=%s AND product_revision=%s ORDER BY id DESC LIMIT 1''', (product['id'], product['revision'])).fetchone()
            latest_id = product['history'][-1]['id'] if product['history'] else None
            locations = db.execute('SELECT url FROM offers WHERE search_id=%s',(latest_id,)).fetchall() if latest_id else []
            product['coverage'] = dict(confirmed_stores=len({urlparse(row['url']).hostname.removeprefix('www.') for row in locations}))
    return jsonify(rows)


@app.get('/api/monitoring')
def monitoring():
    from notifications import status
    current=schedule.now()
    with connect() as db:
        reserved=db.execute('SELECT coalesce(sum(units),0) AS n FROM api_reservations WHERE month=%s',
                            (current.date().replace(day=1),)).fetchone()['n']
        alerts=db.execute('SELECT id,body,url,status,error,created_at,sent_at FROM notifications ORDER BY id DESC LIMIT 20').fetchall()
    return jsonify(phase=schedule.phase(),next_run=schedule.next_run().isoformat(),timezone='America/Sao_Paulo',
                   automatic_budget=200,total_budget=250,reserved_units=reserved,telegram=status(),alerts=alerts)


@app.route('/api/telegram',methods=['GET','POST','DELETE'])
def telegram_settings():
    import notifications
    if request.method=='POST':
        return jsonify(notifications.prepare(text_field(json_body(),'token',True,200)))
    if request.method=='DELETE':
        notifications.config_path().unlink(missing_ok=True)
    return jsonify(notifications.status())


@app.post('/api/telegram/link')
def telegram_link():
    from notifications import link_chat
    return jsonify(link_chat())


@app.post('/api/telegram/test')
def telegram_test():
    from notifications import send_test
    send_test()
    return jsonify(ok=True)


@app.route('/api/search-provider', methods=['GET', 'PUT', 'DELETE'])
def search_provider():
    if request.method == 'PUT':
        key = text_field(json_body(), 'api_key', True, 256)
        # Validate before persistence; do not overwrite a working configuration on failure.
        if not re.fullmatch(r'[a-zA-Z0-9_-]{32,256}', key):
            raise ValueError('Informe uma chave de API válida.')
        account = free_account(key)
        save_key(key)
        return jsonify(configured=True, provider='SerpApi', **account)
    if request.method == 'DELETE':
        key_path().unlink(missing_ok=True)
    return jsonify(configured=bool(read_key()), provider='SerpApi')


@app.route('/api/products', methods=['POST'])
@app.route('/api/products/<int:product_id>', methods=['PUT'])
def save_product(product_id=None):
    data = json_body()
    values = (text_field(data, 'name', True, 160), text_field(data, 'brand', True, 120),
              text_field(data, 'model', True, 120), text_field(data, 'requirements', limit=5000),
              condition(data.get('desired_condition', 'Novo')), cents(data.get('target')),
              text_field(data, 'category', True, 80), text_field(data, 'desired_size', limit=30),
              text_field(data, 'preferred_colors', limit=160))
    with connect() as db:
        if product_id is None:
            row = db.execute('''INSERT INTO products(name,brand,model,requirements,desired_condition,target_cents,category,desired_size,preferred_colors)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id''', values).fetchone()
            product_id = row['id']
        else:
            result = db.execute('''UPDATE products SET name=%s,brand=%s,model=%s,requirements=%s,
                desired_condition=%s,target_cents=%s,category=%s,desired_size=%s,preferred_colors=%s,
                revision=revision + CASE WHEN (brand,model,requirements,desired_condition,desired_size,preferred_colors)
                IS DISTINCT FROM (%s,%s,%s,%s,%s,%s) THEN 1 ELSE 0 END WHERE id=%s''',
                (*values, values[1], values[2], values[3], values[4], values[7], values[8], product_id))
            if result.rowcount == 0:
                return fail('Produto não encontrado.', 404)
        product = db.execute('SELECT * FROM products WHERE id=%s FOR UPDATE', (product_id,)).fetchone()
        if not product['archived']:
            job = enqueue_collection(db, product, trigger='product_saved')
        else:
            job = None
    return jsonify(id=product_id,collection=job), 201 if request.method == 'POST' else 200


@app.patch('/api/products/<int:product_id>/archive')
def archive_product(product_id):
    archived = flag(json_body(), 'archived')
    with connect() as db:
        result = db.execute('UPDATE products SET archived=%s WHERE id=%s', (archived, product_id))
        if result.rowcount == 0:
            return fail('Produto não encontrado.', 404)
    return jsonify(ok=True)


@app.post('/api/products/<int:product_id>/photos')
def upload_photo(product_id):
    files = request.files.getlist('photos')
    if not files or len(files) > 6:
        raise ValueError('Envie de 1 a 6 fotos por vez.')
    images = []
    try:
        for file in files:
            with Image.open(file.stream) as original:
                if original.format not in ('JPEG', 'PNG', 'WEBP'):
                    raise ValueError('Use fotos JPG, PNG ou WebP.')
                image = ImageOps.exif_transpose(original)
                image.thumbnail((1600, 1600))
                image = image.convert('RGB')
                content = io.BytesIO()
                image.save(content, 'JPEG', quality=85)
                images.append((f'{uuid.uuid4().hex}.jpg', content.getvalue()))
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        raise ValueError('Não foi possível ler a foto. Use JPG, PNG ou WebP.')
    written = []
    try:
        with connect() as db:
            if not db.execute('SELECT id FROM products WHERE id=%s FOR UPDATE', (product_id,)).fetchone():
                return fail('Produto não encontrado.', 404)
            count = db.execute('SELECT count(*) AS n FROM photos WHERE product_id=%s', (product_id,)).fetchone()['n']
            if count + len(images) > 12:
                raise ValueError('Limite de 12 fotos por produto.')
            for filename, content in images:
                path = UPLOADS / filename
                path.write_bytes(content)
                written.append(path)
                db.execute('INSERT INTO photos(product_id,filename) VALUES(%s,%s)', (product_id, filename))
    except Exception:
        for path in written:
            path.unlink(missing_ok=True)
        raise
    return jsonify(ok=True), 201


@app.get('/photos/<filename>')
def photo(filename):
    if not re.fullmatch(r'[a-f0-9]{32}\.jpg', filename):
        return fail('Foto não encontrada.', 404)
    return send_from_directory(UPLOADS, filename)


@app.post('/api/products/<int:product_id>/searches')
def create_search(product_id):
    data = json_body()
    try:
        verified = datetime.fromisoformat(data.get('verified_at', ''))
        if verified.tzinfo is None or verified > datetime.now(timezone.utc):
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError('Informe a data de verificação, com fuso horário, sem estar no futuro.')
    notes = text_field(data, 'notes', limit=3000)
    entries = data.get('offers')
    if not isinstance(entries, list) or not 1 <= len(entries) <= 50:
        raise ValueError('Inclua de 1 a 50 ofertas na pesquisa.')
    offers = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError('Oferta inválida.')
        url = text_field(entry, 'url', True, 2000)
        parsed = urlparse(url)
        if parsed.scheme not in ('https', 'http') or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('Informe um link HTTP ou HTTPS válido para a oferta.')
        price, shipping, discount = (cents(entry.get(key, 0)) for key in ('price', 'shipping', 'discount'))
        final = price + shipping - discount
        if final < 0:
            raise ValueError('O desconto não pode ultrapassar o preço mais o frete.')
        offers.append((text_field(entry, 'store', True, 160), url, price, shipping, discount, final,
                       text_field(entry, 'payment', True, 160), condition(entry.get('condition', 'Novo')),
                       flag(entry, 'imported')))
    with connect() as db:
        product = db.execute('SELECT archived,revision FROM products WHERE id=%s FOR UPDATE', (product_id,)).fetchone()
        if not product:
            return fail('Produto não encontrado.', 404)
        if product['archived']:
            raise ValueError('Reative o produto antes de registrar uma pesquisa.')
        search_id = db.execute('INSERT INTO searches(product_id,verified_at,notes,product_revision) VALUES(%s,%s,%s,%s) RETURNING id',
                               (product_id, verified, notes, product['revision'])).fetchone()['id']
        cep=db.execute('SELECT cep FROM preferences WHERE id=1').fetchone()['cep']
        for offer in offers:
            db.execute('''INSERT INTO offers(search_id,store,url,price_cents,shipping_cents,discount_cents,
                final_cents,payment,condition,imported,shipping_cep) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''', (search_id, *offer,cep))
    from notifications import evaluate_search
    evaluate_search(search_id)
    return jsonify(id=search_id), 201


@app.get('/api/products/<int:product_id>/offers')
def product_offers(product_id):
    with connect() as db:
        if not db.execute('SELECT id FROM products WHERE id=%s', (product_id,)).fetchone():
            return fail('Produto não encontrado.', 404)
        rows = db.execute('''SELECT o.*, s.verified_at, s.notes, s.source, s.product_revision FROM offers o
            JOIN searches s ON s.id=o.search_id WHERE s.product_id=%s
            ORDER BY s.verified_at DESC, s.id DESC, o.final_cents, o.id''', (product_id,)).fetchall()
    return jsonify(rows)


@app.route('/api/preferences', methods=['GET', 'PUT'])
def preferences():
    with connect() as db:
        if request.method == 'PUT':
            data = json_body()
            cep = re.sub(r'\D', '', text_field(data, 'cep', limit=9))
            if cep and len(cep) != 8:
                raise ValueError('O CEP deve ter oito dígitos.')
            payments = data.get('payments', [])
            if not isinstance(payments, list) or not payments or any(p not in ('Pix', 'Cartão de crédito', 'Boleto', 'Débito') for p in payments):
                raise ValueError('Selecione ao menos uma forma de pagamento.')
            db.execute('UPDATE preferences SET cep=%s,payments=%s,imported=%s,used=%s WHERE id=1',
                       (cep, payments, flag(data, 'imported'), flag(data, 'used')))
        row = db.execute('SELECT * FROM preferences WHERE id=1').fetchone()
    return jsonify(row)


def excel_text(value):
    if isinstance(value, str):
        # Export arbitrary user input as text, never spreadsheet formulas.
        value = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', value)
        return "'" + value if value.lstrip().startswith(('=', '+', '-', '@')) else value
    return value


@app.get('/api/export.xlsx')
def export_excel():
    with connect() as db:
        # All sheets describe one consistent database snapshot.
        db.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        products = db.execute('SELECT * FROM products ORDER BY id').fetchall()
        offers = db.execute('''SELECT p.id AS product_id,p.name,s.id AS search_id,s.verified_at,s.notes,s.source,o.*
            FROM offers o JOIN searches s ON s.id=o.search_id JOIN products p ON p.id=s.product_id
            ORDER BY s.verified_at,s.id,o.id''').fetchall()
        prefs = db.execute('SELECT * FROM preferences WHERE id=1').fetchone()
    wb = Workbook()
    wb.remove(wb.active)
    sheets = [
        ('Produtos', ['ID','Produto','Marca','Modelo','Categoria','Características obrigatórias','Condição desejada','Preço desejado (R$)','Arquivado','Tamanho desejado','Cores aceitas'],
         [[p['id'],p['name'],p['brand'],p['model'],p['category'],p['requirements'],p['desired_condition'],p['target_cents']/100,'Sim' if p['archived'] else 'Não',p['desired_size'],p['preferred_colors']] for p in products], [8]),
        ('Ofertas', ['ID produto','Produto','ID pesquisa','Verificação (UTC)','Loja','Link','Preço (R$)','Frete (R$)','Desconto (R$)','Total (R$)','Pagamento','Condição','Importado','Origem','Observações','Variação da oferta'],
         [[o['product_id'],o['name'],o['search_id'],o['verified_at'].isoformat(),o['store'],o['url'],o['price_cents']/100,o['shipping_cents']/100 if o['shipping_cents'] is not None else None,o['discount_cents']/100,o['final_cents']/100 if o['final_cents'] is not None else None,o['payment'],o['condition'],('Sim' if o['imported'] else 'Não') if o['imported_known'] else 'Não informado',o['source'],o['notes'],o['title']] for o in offers], [7,8,9,10]),
        ('Preferências', ['CEP','Pagamentos aceitos','Aceita importados','Aceita usados'],
         [[prefs['cep'],', '.join(prefs['payments']),'Sim' if prefs['imported'] else 'Não','Sim' if prefs['used'] else 'Não']], [])]
    for name, headers, rows, money_columns in sheets:
        ws = wb.create_sheet(name)
        ws.append(headers)
        for row in rows:
            ws.append([excel_text(v) for v in row])
        ws.freeze_panes = 'A2'
        ws.auto_filter.ref = ws.dimensions
        for cell in ws[1]:
            cell.font = Font(bold=True, color='FFFFFF')
            cell.fill = PatternFill('solid', fgColor='146D57')
        for column in ws.columns:
            ws.column_dimensions[column[0].column_letter].width = min(55, max(16, max(len(str(c.value or '')) for c in column) + 2))
        for col in money_columns:
            for row in ws.iter_rows(min_row=2, min_col=col, max_col=col):
                row[0].number_format = '"R$" #,##0.00'
    stream = io.BytesIO()
    wb.save(stream)
    stream.seek(0)
    return send_file(stream, mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                     as_attachment=True, download_name=f'preco-certo-{datetime.now(timezone.utc):%Y-%m-%d}.xlsx')


initialize()
