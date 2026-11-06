import json
import os
import re
import uuid
from pathlib import Path
from urllib.parse import urlencode


def key_path():
    return Path(os.environ.get('UPLOAD_DIR', './data/photos')).parent / 'search-api-key'


def read_key():
    try:
        return key_path().read_text(encoding='utf-8').strip()
    except FileNotFoundError:
        return ''


def save_key(key):
    if not isinstance(key, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{32,256}', key):
        raise ValueError('Informe uma chave de API válida.')
    target = key_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f'.search-key-{uuid.uuid4().hex}')
    try:
        with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as output:
            output.write(key)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def free_account(key):
    from discovery import read_public
    try:
        body, _ = read_public('https://serpapi.com/account.json?' + urlencode({'api_key': key}))
        data = json.loads(body)
        if data.get('error') or data.get('account_status') != 'Active':
            raise ValueError('Chave inválida ou conta inativa.')
        if data.get('plan_monthly_price') != 0 or 'free' not in str(data.get('plan_id', '')).lower() + str(data.get('plan_name', '')).lower():
            raise ValueError('Use uma conta no plano Free. A busca está limitada ao plano gratuito.')
        remaining = int(data.get('plan_searches_left', 0))
        if remaining <= 0:
            raise ValueError('A cota gratuita acabou. Aguarde a renovação no provedor.')
        return {'plan': 'Free', 'remaining': remaining}
    except ValueError:
        raise
    except Exception:
        raise ValueError('Não foi possível validar a chave no provedor. Tente novamente.') from None
