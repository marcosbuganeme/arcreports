"""Cliente da API 4Biz (Comunix): só o agregado por dia e serviço é gravado; telefone, documento,
agente, conta, CTI e identificador da ligação são descartados na leitura."""
from collections import defaultdict
from datetime import date, datetime
import json
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

from sqlalchemy import delete, select

from app.config import get_settings
from app.crypto import decrypt_password, encrypt_password
from app.models import AdminActionLog, UraApiDaily
from app.system_config import get_system_config_value, set_system_config_value

KEYS = {'user': 'comunix_user', 'password': 'comunix_password', 'login_url': 'comunix_login_url',
        'query_url': 'comunix_query_url', 'project': 'comunix_project'}
DEFAULTS = {'login_url': 'https://cco.4biz.globalweb.com.br/4biz/services/login',
            'query_url': 'https://cco.4biz.globalweb.com.br/cit-esi-web/rest/esi/execute/comunix_request_det_go',
            'project': 'DET_GO'}
RESULT_KEY = 'webservice-personalizado-provider-exportacao-quality'
ANSWER_LIMIT_SECONDS = 30
settings = get_settings()


def load_config(db):
    config = {name: get_system_config_value(db, key, DEFAULTS.get(name, '')) or '' for name, key in KEYS.items()}
    config['password'] = decrypt_password(config['password'], settings.encryption_key) if config['password'] else ''
    return config


def save_config(db, data, username):
    for name in ('user', 'login_url', 'query_url', 'project'):
        value = (data.get(name) or '').strip()
        if not value:
            raise ValueError('Preencha usuário, endereços e projeto da API.')
        if name.endswith('url') and not value.startswith('https://'):
            raise ValueError('Os endereços da API devem usar https.')
        set_system_config_value(db, KEYS[name], value)
    if data.get('password'):
        set_system_config_value(db, KEYS['password'], encrypt_password(data['password'], settings.encryption_key))
    db.add(AdminActionLog(username=username, action='save_comunix_config', table_name='system_config', status='success',
                          message='Configuração da API 4Biz atualizada (senha não registrada).'))
    db.commit()


def _post(url, body, headers, timeout):
    request = urllib.request.Request(url, data=json.dumps(body).encode(), method='POST',
                                     headers={'Content-Type': 'application/json', **headers})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _session_id(raw):
    """O login devolve XML com SessionID; aceita também a mesma chave em JSON."""
    try:
        return ET.fromstring(raw).findtext('.//SessionID')
    except ET.ParseError:
        pass
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    stack = [payload]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            if isinstance(item.get('SessionID'), str):
                return item['SessionID']
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return None


def _calls(payload):
    if isinstance(payload, dict):
        if isinstance(payload.get(RESULT_KEY), list):
            return payload[RESULT_KEY]
        for value in payload.values():
            found = _calls(value)
            if found is not None:
                return found
    return None


def aggregate(calls):
    totals = defaultdict(lambda: [0, 0])
    for call in calls:
        moment = datetime.strptime(str(call['data_hora'])[:19].replace('/', '-'), '%Y-%m-%d %H:%M:%S')
        key = (moment.date(), str(call['servico']).strip())
        totals[key][0] += 1
        if int(call.get('tempo_espera') or 0) <= ANSWER_LIMIT_SECONDS:
            totals[key][1] += 1
    return {key: tuple(value) for key, value in totals.items()}


def fetch(db, start: date, end: date, project: str, username: str, post=_post, timeout=60):
    config = load_config(db)
    if not config['user'] or not config['password']:
        raise ValueError('Configure usuário e senha da API 4Biz antes de consultar.')
    try:
        token = _session_id(post(config['login_url'], {'userName': config['user'], 'password': config['password']}, {}, timeout))
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ValueError('Falha no login da API 4Biz. Confira usuário, senha e rede.') from exc
    if not token:
        raise ValueError('A API 4Biz não devolveu sessão. Confira usuário e senha.')
    try:
        raw = post(config['query_url'], {'data_inicial': start.isoformat(), 'data_final': end.isoformat(), 'projeto': config['project']},
                   {'Authorization': 'Bearer ' + token, 'Accept': 'application/json'}, timeout)
        calls = _calls(json.loads(raw))
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError, OSError) as exc:
        raise ValueError('Falha na consulta da API 4Biz.') from exc
    if calls is None:
        raise ValueError('Resposta da API 4Biz sem a lista de ligações esperada.')
    totals = {key: value for key, value in aggregate(calls).items() if start <= key[0] <= end}
    db.execute(delete(UraApiDaily).where(UraApiDaily.project == project, UraApiDaily.day >= start, UraApiDaily.day <= end))
    for (day, service), (answered, answered_30) in sorted(totals.items()):
        db.add(UraApiDaily(project=project, service_name=service, day=day, answered=answered, answered_30=answered_30, username=username))
    db.add(AdminActionLog(username=username, action='fetch_comunix_api', table_name='ura_api_daily', status='success',
                          message=f'API 4Biz {start} a {end}: {len(calls)} ligações atendidas agregadas em {len(totals)} dias/serviços.'))
    db.commit()
    return {'calls': len(calls), 'groups': len(totals)}


def confront(db, project, start, end, csv_rows):
    api = {(r.day, r.service_name): r for r in db.scalars(select(UraApiDaily).where(
        UraApiDaily.project == project, UraApiDaily.day >= start, UraApiDaily.day <= end))}
    rows, equal = [], 0
    keys = sorted(set(api) | {(r.day, r.service_name) for r in csv_rows})
    by_csv = {(r.day, r.service_name): r for r in csv_rows}
    for key in keys:
        a, c = api.get(key), by_csv.get(key)
        same = bool(a and c and a.answered == c.answered and a.answered_30 == c.answered_30) or (not a and c and c.answered == 0)
        equal += same
        rows.append({'day': key[0], 'service': key[1], 'api_answered': a.answered if a else None, 'csv_answered': c.answered if c else None,
                     'api_answered_30': a.answered_30 if a else None, 'csv_answered_30': c.answered_30 if c else None, 'equal': same})
    return {'rows': rows, 'equal': equal, 'total': len(rows), 'api_groups': len(api),
            'answered': sum(r.answered for r in api.values()), 'answered_30': sum(r.answered_30 for r in api.values())}
