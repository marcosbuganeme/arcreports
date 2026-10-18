"""Apuração da competência: todos os INS, IND e o TRS45, pontos da Tabela 25, faturas e teto."""
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from app.billing_rules import CONTRACT_START, Competence, adjusted_goal, knowledge_goal, pao_factor
from app.business_calendar import PROVISIONAL_2027, business_days, holiday_set, seed_holidays
from app.contract_registries import availability, ins11, project_quality, turnover
from app.contract_report import ura_report
from app.contract_scoring import PENDING_SCALE, POLICIES, adjustment, indicator_points, occurrence_points
from app.comunix import confront
from app.database import local_engine
from app.glpi_contract import PRIORITIES, evaluate, load_snapshot
from app.metrics import measure
from app.models import ContractEntityMap, ContractHoliday, ContractInvoiceMap, UraApiDaily, UraDaily
from app.ura import normalize_project

INVOICES = {'item01': 'Item 01 — Central de Serviços (equipes 1 a 3)',
            'item02': 'Item 02 — Infraestrutura de TIC (equipes 4 a 12)'}
INVOICE_CHOICES = {'item01': 'Item 01, medido nos chamados e equipes do Item 01',
                   'item02': 'Item 02, medido nos chamados e equipes do Item 02',
                   'item01_geral': 'Item 01, medido sobre todos os chamados',
                   'item02_geral': 'Item 02, medido sobre todos os chamados',
                   'separado': 'Um resultado por fatura, cada um no seu escopo',
                   'a_confirmar': 'A confirmar (fora das duas faturas)'}
SCOPE_LABELS = {'item01': 'Central de Serviços', 'item02': 'Infraestrutura de TIC', 'geral': 'Geral'}
PROPOSAL = 'Proposta enviada à contratada em 17/10/2026 (6ª mensagem), a confirmar'
DEFAULT_INVOICE_MAP = {
    'INS01': 'separado', 'INS02': 'separado', 'INS03': 'separado', 'INS04': 'separado', 'INS05': 'separado',
    'INS06': 'separado', 'INS07': 'separado', 'INS08': 'separado', 'INS09': 'item02', 'INS10': 'item01',
    'INS11': 'separado', 'INS12': 'item01', 'INS13': 'item01', 'IND01': 'item02', 'IND02': 'item02',
}
ANSWER_18_10 = 'Resposta da contratada em 18/10/2026'
CONFIRMED_SOURCES = {
    'INS01': 'TR, INS01: medido separado para a Central de Serviços e a Infraestrutura de TIC',
    **{code: f'{ANSWER_18_10} (2-A: um resultado por item)' for code in ('INS02', 'INS03', 'INS04', 'INS05', 'INS06', 'INS07')},
    'INS08': f'{ANSWER_18_10} (3-C: pela fatura do item de cada projeto)',
    'INS09': 'TR, INS09: equipes 05 a 12 (Infraestrutura de TIC)',
    'INS10': 'Resposta da contratada em 17/10/2026: equipes 01 a 03 (Central de Serviços)',
    'INS11': 'Resposta da contratada em 17/10/2026 (5ª mensagem): separado por contrato',
}
DEFAULT_ENTITY_MAP = {4: ('item01', 'Central TI'), 7: ('item02', 'Infraestrutura'), 8: ('item02', 'Manutenção')}
LABELS = {
    'INS01': 'Satisfação do usuário', 'INS02': 'Início de tratamento no prazo', 'INS03': 'Solução de incidentes no prazo',
    'INS04': 'Solução de requisições no prazo', 'INS05': 'Reabertura em até 4 dias úteis', 'INS06': 'Vínculo com a base de conhecimento',
    'INS07': 'Vínculo com o CMDB', 'INS08': 'Qualidade de projetos e demandas planejadas', 'INS09': 'Rotatividade na infraestrutura',
    'INS10': 'Rotatividade na Central de Serviços', 'INS11': 'Disponibilidade de profissionais', 'INS12': 'Ligações atendidas',
    'INS13': 'Ligações atendidas em até 30 s', 'IND01': 'Disponibilidade de serviços críticos', 'IND02': 'Disponibilidade de serviços não críticos',
}
GLPI_GOALS = {'INS01': ('80', 'minimum'), 'INS02': ('95', 'minimum'), 'INS03': ('95', 'minimum'), 'INS04': ('95', 'minimum'),
              'INS05': ('8', 'maximum')}


def seed_contract_defaults(db):
    seed_holidays(db)
    existing = {r.indicator: r for r in db.scalars(select(ContractInvoiceMap))}
    for code, invoice in DEFAULT_INVOICE_MAP.items():
        source = CONFIRMED_SOURCES.get(code)
        row = existing.get(code)
        if row is None:
            db.add(ContractInvoiceMap(indicator=code, invoice=invoice, confirmed=source is not None, source=source or PROPOSAL))
        elif source and not row.confirmed and row.source == PROPOSAL:
            row.invoice, row.confirmed, row.source = invoice, True, source
    entities = set(db.scalars(select(ContractEntityMap.entity_id)))
    for entity, (invoice, name) in DEFAULT_ENTITY_MAP.items():
        if entity not in entities:
            db.add(ContractEntityMap(entity_id=entity, invoice=invoice, confirmed=False, source=f'{PROPOSAL}: entidade {name}'))
    db.commit()


def period_end(period: Competence):
    return datetime.combine(period.end + timedelta(days=1), time.min)


def portal_now():
    return datetime.now(ZoneInfo('America/Sao_Paulo')).replace(tzinfo=None)


def default_cut(period: Competence, now: datetime):
    return min(now.replace(second=0, microsecond=0), period_end(period))


def parse_cut(value, period: Competence, now: datetime):
    """Corte fixo (horário de Brasília) para a assinatura da exportação não mudar com o relógio."""
    if not value:
        return default_cut(period, now)
    try:
        cut = datetime.fromisoformat(str(value)).replace(second=0, microsecond=0)
    except ValueError as exc:
        raise ValueError('Corte inválido. Use data e hora no formato AAAA-MM-DDTHH:MM.') from exc
    if cut.tzinfo is not None:
        raise ValueError('Informe o corte no horário de Brasília, sem fuso.')
    start = datetime.combine(period.start, time.min)
    if not start < cut <= period_end(period):
        raise ValueError(f'O corte deve estar dentro da competência {period.label}.')
    return cut


def glpi_database(db):
    from app.glpi_import import get_connection_config, target_import_database
    config = get_connection_config(db, 'glpi')
    if config is None:
        raise LookupError('Conector GLPI não configurado.')
    return target_import_database(config)


def load_glpi(db, period, cut, holidays, entity_invoice):
    try:
        database = glpi_database(db)
        with local_engine.connect() as connection:
            snapshot, missing = load_snapshot(connection, database, period, cut, portal_now())
    except (LookupError, ValueError) as exc:
        return None, [], str(exc)
    except Exception as exc:  # banco local indisponível: a apuração mostra o motivo em vez de zero
        return None, [], f'Cópia local do GLPI indisponível: {exc.__class__.__name__}.'
    return evaluate(snapshot, period, holidays, entity_invoice), missing, None


def _row(code, scope, measurement, direction, base_goal, period, priority=None, note=None):
    state = measurement.get('state', 'no_data')
    value, goal = measurement.get('value'), measurement.get('goal')
    points = indicator_points(code, value, goal, direction, period) if state == 'measured' else None
    return {'code': code, 'label': LABELS[code], 'scope': scope, 'priority': priority, 'state': state, 'value': value,
            'numerator': measurement.get('numerator'), 'denominator': measurement.get('denominator'), 'base_goal': Decimal(base_goal),
            'goal': goal, 'direction': direction, 'meets_goal': measurement.get('meets_goal'), 'points': points,
            'note': note or measurement.get('note'), 'missing': measurement.get('missing') or []}


def _no_data(code, scope, period, base_goal, direction, reason, priority=None):
    goal = knowledge_goal(period) if code in ('INS06', 'INS07') else adjusted_goal(base_goal, direction, period)
    return _row(code, scope, {'state': 'no_data', 'goal': goal, 'missing': [reason]}, direction, base_goal, period, priority)


def _sla_note(code, measurement):
    note = f"{measurement['without_sla']} sem SLA"
    if code != 'INS02':
        note += f" e {measurement['no_resolution_category']} em categoria sem prazo de solução"
    return note + ' fora do denominador'


def indicator_rows(db, period, glpi, glpi_error, ura):
    rows = []
    for scope in ('item01', 'item02', 'geral'):
        data = glpi['scopes'][scope] if glpi else None
        for code in ('INS01', 'INS02', 'INS03', 'INS04', 'INS05', 'INS06', 'INS07'):
            base, direction = GLPI_GOALS.get(code, (str(knowledge_goal(period)), 'minimum'))
            if code in ('INS06', 'INS07'):
                base = str(knowledge_goal(period))
            if code in ('INS02', 'INS03', 'INS04'):
                for p in PRIORITIES:
                    rows.append(_row(code, scope, data[code][p], direction, base, period, p,
                                     note=_sla_note(code, data[code][p]) if data else None)
                                if data else _no_data(code, scope, period, base, direction, glpi_error, p))
            else:
                note = None
                if data and code == 'INS05':
                    note = f"{data[code]['events']} reaberturas em {data[code]['concluded']} chamados concluídos (inclui chamados sem SLA)"
                if code == 'INS06' or code == 'INS07':
                    note = 'Meta pela progressão trimestral, sem o fator PAO; os pontos têm o fator'
                rows.append(_row(code, scope, data[code], direction, base, period, note=note) if data
                            else _no_data(code, scope, period, base, direction, glpi_error))
    for scope in ('item01', 'item02', 'geral'):
        rows.append(_row('INS08', scope, project_quality(db, period, scope), 'minimum', '80', period))
    for code in ('INS09', 'INS10'):
        m = turnover(db, period, code)
        rows.append(_row(code, m['scope'], m, 'maximum', '15', period, note=m['basis'] if m['state'] != 'not_in_force' else 'Vigente a partir da competência de dezembro (3º mês)'))
    for scope in ('item01', 'item02', 'geral'):
        m = ins11(db, period, scope)
        rows.append(_row('INS11', scope, m, 'minimum', '98', period,
                         note=f"N = {m['minimum_staff']} profissionais; D = {m['business_days']} dias úteis"))
    for metric in ura['metrics']:
        rows.append(_row(metric['code'], 'item01', metric, 'minimum', '95', period,
                         note='Série diária importada da URA' + (' (parcial)' if ura['partial'] else '')))
    for code in ('IND01', 'IND02'):
        m = availability(db, period, code)
        rows.append(_row(code, 'item02', m, 'minimum', str(m['base_goal']), period))
    return rows


def _pick(rows, code, scope):
    exact = [r for r in rows if r['code'] == code and r['scope'] == scope]
    if exact:
        return exact
    general = [r for r in rows if r['code'] == code and r['scope'] == 'geral']
    return general or [r for r in rows if r['code'] == code]


def _sum(rows, policy):
    return sum((r['points'][policy] for r in rows if r['points'] and r['points'][policy] is not None), Decimal(0))


def _general(rows, code):
    return [r for r in rows if r['code'] == code and r['scope'] == 'geral'] or [r for r in rows if r['code'] == code]


def assigned_rows(rows, invoice_map, invoice):
    """Linhas que entram na fatura conforme o mapa indicador → fatura (um indicador nunca entra duas vezes)."""
    chosen = []
    for code in dict.fromkeys(r['code'] for r in rows):
        target = invoice_map.get(code, 'a_confirmar')
        if target == invoice:
            chosen += _pick(rows, code, invoice)
        elif target == invoice + '_geral':
            chosen += _general(rows, code)
        elif target == 'separado':
            chosen += [r for r in rows if r['code'] == code and r['scope'] == invoice]
    return chosen


def unsplittable(rows, invoice_map, code):
    """'Separado' exige um resultado por fatura; indicador só com resultado geral (INS08) não se divide."""
    return invoice_map.get(code) == 'separado' and not any(r['code'] == code and r['scope'] in INVOICES for r in rows)


def totals(rows, trs45, invoice_map, period):
    result = {}
    for invoice in INVOICES:
        occurrences = sum(1 for t in trs45 if t['scope'] == invoice)
        result[invoice] = _block(assigned_rows(rows, invoice_map, invoice), occurrences, period)
    pending_codes = [c for c in dict.fromkeys(r['code'] for r in rows) if invoice_map.get(c, 'a_confirmar') not in INVOICE_CHOICES
                     or invoice_map.get(c, 'a_confirmar') == 'a_confirmar' or unsplittable(rows, invoice_map, c)]
    unassigned = {'codes': pending_codes}
    for scope in ('item01', 'item02', 'geral'):
        picked = []
        for code in pending_codes:
            picked += [r for r in rows if r['code'] == code and r['scope'] == scope] or ([] if scope != 'geral' else _general(rows, code))
        unassigned[scope] = {p: _sum(picked, p) for p in POLICIES}
    result['nao_atribuido'] = unassigned
    trs_unassigned = sum(1 for t in trs45 if t['scope'] not in INVOICES)
    result['trs45_nao_atribuido'] = occurrence_points('TRS45', trs_unassigned, period)
    general = []
    for code in dict.fromkeys(r['code'] for r in rows):
        general += _general(rows, code)
    result['geral'] = _block(general, len(trs45), period)
    return result


def _block(rows, occurrences, period):
    trs = occurrence_points('TRS45', occurrences, period)
    block = {'indicators': len(rows), 'codes': list(dict.fromkeys(r['code'] for r in rows)), 'trs45': trs,
             'without_result': sum(1 for r in rows if r['state'] not in ('measured', 'not_in_force') or (r['points'] and r['points']['pending']))}
    for policy in POLICIES:
        block[policy] = adjustment(_sum(rows, policy) + trs['weighted'])
    return block


def api_ura(db, period, project):
    project = normalize_project(project)
    answered, answered_30, groups = db.execute(select(func.sum(UraApiDaily.answered), func.sum(UraApiDaily.answered_30), func.count())
                                               .where(UraApiDaily.project == project, UraApiDaily.day >= period.start,
                                                      UraApiDaily.day <= period.end)).one()
    csv_rows = db.scalars(select(UraDaily).where(UraDaily.project == project, UraDaily.day >= period.start, UraDaily.day <= period.end)).all()
    comparison = confront(db, project, period.start, period.end, csv_rows) if groups else None
    return {'groups': groups, 'INS13': measure(answered_30, answered, goal=adjusted_goal('95', 'minimum', period)),
            'confront': comparison, 'rule': 'Informativo: ligações atendidas da API 4Biz, agregadas por dia e serviço; não entra nos pontos'}


def pending_items(result):
    items = []
    if result['glpi_error']:
        items.append(('GLPI', result['glpi_error']))
    for table in result['glpi_missing_tables']:
        items.append(('GLPI', f'Tabela {table} ausente na cópia local: o indicador que depende dela fica sem dado.'))
    seen = set()
    for row in result['rows']:
        for reason in row['missing']:
            if (row['code'], reason) not in seen and reason:
                seen.add((row['code'], reason))
                items.append((row['code'], f'Cadastro pendente: {reason}.'))
    for code, text in PENDING_SCALE.items():
        items.append((code, text))
    for code, invoice in sorted(result['invoice_map'].items()):
        if code in result['totals']['nao_atribuido']['codes'] and invoice == 'separado':
            items.append((code, 'Marcado como um resultado por fatura, mas só tem resultado geral: escolha uma fatura; os pontos ficam fora das duas.'))
        elif invoice == 'a_confirmar':
            items.append((code, 'Fatura do indicador a confirmar com a contratada; os pontos ficam fora das duas faturas.'))
        elif not result['invoice_confirmed'].get(code):
            items.append((code, f'Fatura proposta ({INVOICE_CHOICES.get(invoice, invoice)}) ainda não confirmada.'))
    for entity in result['entity_map']:
        if not entity.confirmed:
            items.append(('Entidades', f'Entidade {entity.entity_id} → {entity.invoice}: proposta, a confirmar.'))
    if result['glpi']:
        diagnostics = result['glpi']['diagnostics']
        if diagnostics['unassigned_entity']:
            items.append(('Entidades', f"{len(diagnostics['unassigned_entity'])} chamados solucionados em entidade sem fatura (raiz ou outra)."))
        if diagnostics['transferred']:
            items.append(('Entidades', f"{len(diagnostics['transferred'])} chamados transferidos entre entidades: fatura pela entidade final, a confirmar."))
        if result['glpi']['reopen_provisional']:
            items.append(('INS05', 'Provisório: reaberturas de conclusões do fim da competência só fecham em '
                          + result['glpi']['reopen_window_end'].strftime('%d/%m/%Y %H:%M') + ' (4 dias úteis).'))
    items.append(('INS05', 'Denominador com todos os chamados concluídos, inclusive os sem SLA (a confirmar).'))
    items.append(('P1+P2', 'Excedentes do limite de 60% (os últimos abertos) ficam com prazo e faixa de P3; resposta da contratada '
                  'de 18/10/2026, a confirmar com o Detran.'))
    if result['period'].end.year >= 2027 and not result['holidays_confirmed_2027']:
        items.append(('Feriados', 'Feriados estaduais, municipais e pontos facultativos de 2027 ainda sem o decreto (datas previstas).'))
    return items


def plain(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Competence):
        return value.label
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        items = [plain(v) for v in value]
        return sorted(items, key=str) if isinstance(value, (set, frozenset)) else items
    return value


def evidence(result):
    """Memória completa e estável da apuração (o corte é parte dela, não o relógio)."""
    glpi = result['glpi']
    return plain({
        'cut': result['cut'], 'business_days': result['business_days'], 'holidays': result['holidays_in_period'],
        'factor': result['factor'],
        'indicators': [{k: v for k, v in r.items() if k != 'label'} for r in result['rows']],
        'trs45': result['trs45'],
        'reopen_events': glpi['reopen_events'] if glpi else [],
        'p1p2': {s: glpi['scopes'][s]['P1P2'] for s in glpi['scopes']} if glpi else None,
        'diagnostics': glpi['diagnostics'] if glpi else None,
        'glpi_error': result['glpi_error'], 'glpi_missing_tables': result['glpi_missing_tables'],
        'invoice_map': {k: {'invoice': v, 'confirmed': result['invoice_confirmed'].get(k)} for k, v in sorted(result['invoice_map'].items())},
        'entity_map': {r.entity_id: {'invoice': r.invoice, 'confirmed': r.confirmed} for r in result['entity_map']},
        'totals': result['totals'], 'api_ura': {k: v for k, v in result['api_ura'].items() if k != 'confront'},
        'pending': [f'{a}: {b}' for a, b in result['pending']],
    })


def build(db, period: Competence, project, cut: datetime):
    holidays = holiday_set(db, CONTRACT_START - timedelta(days=10), period.end + timedelta(days=60))
    days = business_days(period.start, period.end, holidays)
    entity_rows = db.scalars(select(ContractEntityMap)).all()
    entity_invoice = {r.entity_id: r.invoice for r in entity_rows if r.invoice in INVOICES}
    invoice_map = {r.indicator: r.invoice for r in db.scalars(select(ContractInvoiceMap))}
    glpi, missing_tables, glpi_error = load_glpi(db, period, cut, holidays, entity_invoice)
    ura = ura_report(db, period, project)
    rows = indicator_rows(db, period, glpi, glpi_error, ura)
    trs45 = glpi['trs45'] if glpi else []
    regional_2027 = db.scalar(select(func.count()).select_from(ContractHoliday).where(
        ContractHoliday.day >= date(2027, 1, 1), ContractHoliday.day <= date(2027, 12, 31),
        ContractHoliday.kind.in_(('estadual', 'municipal', 'ponto_facultativo')), ContractHoliday.source != PROVISIONAL_2027))
    result = {'period': period, 'cut': cut, 'partial': cut < period_end(period), 'business_days': len(days),
              'holidays_in_period': sorted(d for d in holidays if period.start <= d <= period.end),
              'factor': pao_factor(period), 'rows': rows, 'trs45': trs45, 'glpi': glpi, 'glpi_error': glpi_error,
              'glpi_missing_tables': missing_tables, 'ura': ura, 'invoice_map': invoice_map,
              'invoice_confirmed': {r.indicator: r.confirmed for r in db.scalars(select(ContractInvoiceMap))},
              'entity_map': entity_rows, 'totals': totals(rows, trs45, invoice_map, period),
              'api_ura': api_ura(db, period, project), 'holidays_confirmed_2027': bool(regional_2027)}
    result['pending'] = pending_items(result)
    return result
