import hashlib
import json
import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from urllib.request import Request, urlopen

from provider_settings import key_path
from monitoring_schedule import ZONE


def config_path():
    return key_path().with_name('telegram.json')


def read_config():
    try:
        return json.loads(config_path().read_text(encoding='utf-8'))
    except (FileNotFoundError, ValueError):
        return {}


def write_config(config):
    target=config_path()
    target.parent.mkdir(parents=True,exist_ok=True)
    temporary=target.with_name('.telegram-'+secrets.token_hex(12))
    try:
        with os.fdopen(os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'w') as stream:
            json.dump(config,stream)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def telegram_call(config, method, payload=None):
    if method not in ('getMe','getUpdates','sendMessage'):
        raise ValueError('Operação não permitida.')
    request=Request(f'https://api.telegram.org/bot{config["token"]}/{method}',
                    data=json.dumps(payload or {}).encode(),headers={'Content-Type':'application/json'},method='POST')
    try:
        with urlopen(request,timeout=12) as response:
            raw=response.read(1024*1024+1)
        if len(raw)>1024*1024:
            raise ValueError()
        data=json.loads(raw)
        if not data.get('ok'):
            raise ValueError()
        return data.get('result')
    except Exception:
        raise ValueError('O Telegram não concluiu a operação. Confira a conexão, o token e se o bot é exclusivo deste sistema.') from None


def status():
    config=read_config()
    result=dict(configured=bool(config.get('token')),linked=bool(config.get('enabled') and config.get('chat_id')),
                bot_username=config.get('username',''))
    if config.get('nonce') and config.get('pairing_expires',0)>time.time():
        result['pairing_url']=f'https://t.me/{config["username"]}?start={config["nonce"]}'
    return result


def prepare(token):
    if not re.fullmatch(r'\d{6,}:[A-Za-z0-9_-]{20,}',token or ''):
        raise ValueError('Informe o token fornecido pelo BotFather.')
    config=dict(token=token)
    me=telegram_call(config,'getMe')
    if not me.get('is_bot') or not re.fullmatch(r'[A-Za-z0-9_]+',me.get('username','')):
        raise ValueError('O token não corresponde a um bot válido.')
    config.update(username=me['username'],enabled=False,nonce=secrets.token_urlsafe(18),
                  pairing_created=time.time(),pairing_expires=time.time()+900)
    write_config(config)
    return status()


def link_chat():
    config=read_config()
    if not config.get('nonce') or config.get('pairing_expires',0)+86400<time.time():
        raise ValueError('O vínculo expirou. Informe o token novamente para gerar outro link.')
    updates=telegram_call(config,'getUpdates',dict(timeout=0,limit=100,allowed_updates=['message']))
    for update in reversed(updates):
        message=update.get('message',{})
        chat=message.get('chat',{})
        if (chat.get('type')=='private' and message.get('from',{}).get('id')==chat.get('id')
                and message.get('text')=='/start '+config['nonce']
                and config['pairing_created']-5<=message.get('date',0)<=config['pairing_expires']):
            config.update(chat_id=chat['id'],enabled=True,linked_at=datetime.now(timezone.utc).isoformat())
            for key in ('nonce','pairing_created','pairing_expires'):
                config.pop(key,None)
            write_config(config)
            return status()
    if config.get('pairing_expires',0)<time.time():
        raise ValueError('O link expirou sem receber o código válido. Informe o token novamente para gerar outro link.')
    raise ValueError('Abra o link do bot, toque em Iniciar no Telegram e depois confirme o vínculo aqui.')


def send_test():
    config=read_config()
    if not config.get('enabled') or not config.get('chat_id'):
        raise ValueError('Vincule sua conversa antes de enviar o teste.')
    telegram_call(config,'sendMessage',dict(chat_id=config['chat_id'],
        text='Preço Certo: Telegram conectado. Esta é uma mensagem de teste; nenhum preço de produto foi confirmado.'))


def money(value):
    whole,fraction=divmod(value,100)
    return 'R$ '+f'{whole:,}'.replace(',','.')+f',{fraction:02d}'


def evaluate_search(search_id):
    from app import connect
    with connect() as db:
        search=db.execute('SELECT * FROM searches WHERE id=%s',(search_id,)).fetchone()
        if not search or search['verified_at']<datetime.now(timezone.utc)-timedelta(days=1):
            return False
        product=db.execute('SELECT * FROM products WHERE id=%s',(search['product_id'],)).fetchone()
        prefs=db.execute('SELECT * FROM preferences WHERE id=1').fetchone()
        if not re.fullmatch(r'\d{8}',prefs['cep']):
            return False
        if product['archived'] or product['revision']!=search['product_revision']:
            return False
        latest=db.execute('SELECT id FROM searches WHERE product_id=%s AND product_revision=%s ORDER BY verified_at DESC,id DESC LIMIT 1',
                          (product['id'],product['revision'])).fetchone()
        if latest['id']!=search_id:
            return False
        rows=db.execute('''SELECT o.*,s.id AS sid,s.verified_at FROM offers o JOIN searches s ON s.id=o.search_id
            WHERE s.product_id=%s AND s.product_revision=%s AND o.final_cents IS NOT NULL
            AND o.shipping_known AND o.payment_known AND o.shipping_cep=%s''',
            (product['id'],product['revision'],prefs['cep'])).fetchall()
        def acceptable(offer):
            return ((product['desired_condition']=='Qualquer' or offer['condition']==product['desired_condition'])
                    and bool(set(offer['payment'].split(', ')) & set(prefs['payments']))
                    and (prefs['imported'] or (offer['imported_known'] and not offer['imported'])))
        current=[offer for offer in rows if offer['sid']==search_id and acceptable(offer)]
        if not current:
            return False
        best=min(current,key=lambda offer:(offer['final_cents'],offer['id']))
        previous=min((offer['final_cents'] for offer in rows if acceptable(offer)
                      and (offer['verified_at'],offer['sid'])<(search['verified_at'],search_id)),default=None)
        goal=best['final_cents']<=product['target_cents']
        record=previous is not None and best['final_cents']<previous
        if not goal and not record:
            return False
        title='Meta de compra atingida' if goal else 'Novo menor total registrado'
        fingerprint=hashlib.sha256(json.dumps([product['id'],product['revision'],best['url'],best['final_cents'],
                        best['payment'],prefs['cep'],title],ensure_ascii=False).encode()).hexdigest()
        body=(f'{title}\n{product["name"]}\n{best["title"]}\nLoja: {best["store"]}\n'
              f'Produto: {money(best["price_cents"]-best["discount_cents"])}\nFrete: {money(best["shipping_cents"])}\n'
              f'Taxas: {money(best["tax_cents"])}\nTotal: {money(best["final_cents"])}\n'
              f'Pagamento: {best["payment"]}\nMeta com frete: {money(product["target_cents"])}\n'
              f'Verificação: {search["verified_at"].astimezone(ZONE):%d/%m/%Y %H:%M}\n'
              'Preço e estoque podem mudar. Confira as condições na loja.')
        result=db.execute('''INSERT INTO notifications(product_id,search_id,offer_id,fingerprint,body,url)
            VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(fingerprint) DO NOTHING''',
            (product['id'],search_id,best['id'],fingerprint,body,best['url']))
        return result.rowcount==1


def deliver_one():
    from app import connect
    config=read_config()
    if not config.get('enabled') or not config.get('chat_id'):
        return False
    with connect() as db:
        db.execute("UPDATE notifications SET status='not_sent',error='Alerta anterior à conexão do Telegram.' WHERE status='pending' AND created_at<%s",(config['linked_at'],))
        db.execute("UPDATE notifications SET status='pending' WHERE status='sending' AND next_attempt_at < now()-interval '10 minutes'")
        item=db.execute("SELECT * FROM notifications WHERE status='pending' AND next_attempt_at<=now() ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1").fetchone()
        if not item:
            return False
        valid=db.execute('''SELECT n.id FROM notifications n JOIN searches s ON s.id=n.search_id
            JOIN products p ON p.id=n.product_id JOIN offers o ON o.id=n.offer_id CROSS JOIN preferences f
            WHERE n.id=%s AND NOT p.archived AND s.product_revision=p.revision
            AND s.verified_at>now()-interval '1 day'
            AND s.id=(SELECT id FROM searches WHERE product_id=p.id AND product_revision=p.revision ORDER BY verified_at DESC,id DESC LIMIT 1)
            AND (f.imported OR (o.imported_known AND NOT o.imported))
            AND o.shipping_cep=f.cep AND regexp_split_to_array(o.payment, ', ') && f.payments''',(item['id'],)).fetchone()
        if not valid:
            db.execute("UPDATE notifications SET status='not_sent',error='Critérios de compra ou CEP alterados.' WHERE id=%s",(item['id'],))
            return True
        db.execute("UPDATE notifications SET status='sending',attempts=attempts+1,next_attempt_at=now()+interval '5 minutes' WHERE id=%s",(item['id'],))
    try:
        telegram_call(config,'sendMessage',dict(chat_id=config['chat_id'],text=item['body'],
            reply_markup={'inline_keyboard':[[{'text':'Ver produto na loja','url':item['url']}]]}))
        with connect() as db:
            db.execute("UPDATE notifications SET status='sent',sent_at=now(),error='' WHERE id=%s",(item['id'],))
    except ValueError:
        with connect() as db:
            db.execute("UPDATE notifications SET status=%s,error='Falha no envio ao Telegram.',next_attempt_at=now()+interval '5 minutes' WHERE id=%s",
                       ('failed' if item['attempts']>=2 else 'pending',item['id']))
    return True
