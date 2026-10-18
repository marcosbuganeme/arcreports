import json
from datetime import date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app import comunix
from app.billing_rules import Competence
from app.business_calendar import business_days, holiday_set, seed_holidays
from app.contract_apuracao import (CONFIRMED_SOURCES, DEFAULT_INVOICE_MAP, PROPOSAL, build, parse_cut, seed_contract_defaults, totals)
from app.contract_registries import (confirm_registry, import_absences, import_projects, import_staff, ins11, project_quality,
                                     turnover)
from app.contract_scoring import adjustment, indicator_points
from app.glpi_contract import GlpiSnapshot, evaluate
from app.models import ContractEntityMap, ContractInvoiceMap, UraApiDaily, UraDaily, UraDailyRevision
from app.ura import import_ura_csv
from tests.test_ura_import import sample_csv

OCT = Competence.parse('2026-10')


def seeded(db):
    seed_holidays(db)
    db.commit()
    return holiday_set(db, date(2026, 9, 1), date(2027, 12, 31))


def test_calendar_excludes_contract_holidays(db_session):
    holidays = seeded(db_session)
    assert date(2026, 10, 30) in holidays and date(2026, 12, 25) in holidays and date(2026, 10, 12) in holidays
    assert len(business_days(OCT.start, OCT.end, holidays)) == 21
    november = Competence.parse('2026-11')
    days = business_days(november.start, november.end, holidays)
    assert len(days) == 19 and date(2026, 10, 30) not in days and date(2026, 11, 2) not in days


def test_points_policies_factor_and_cap():
    month7 = Competence.parse('2027-04')
    points = indicator_points('INS02', Decimal('92.5'), Decimal(95), 'minimum', month7)
    assert (points['proporcional'], points['truncado']) == (Decimal('12.5'), Decimal(10))
    first = indicator_points('INS02', Decimal('92.5'), Decimal('47.5'), 'minimum', OCT)
    assert first['deviation'] == 0 and first['proporcional'] == 0
    assert indicator_points('INS01', Decimal(78), Decimal(80), 'minimum', month7)['proporcional'] == Decimal(10)
    assert adjustment(Decimal(400)) == {'points': Decimal(400), 'calculated': Decimal(40), 'applied': Decimal(30), 'excess': Decimal(10)}


def ticket(id, opened, solved=None, *, priority=5, type=1, entity=4, sla=1, ttr=None, tto=None, taken=None, request=1, status=5):
    return {'id': str(id), 'entities_id': str(entity), 'date': opened, 'solvedate': solved, 'takeintoaccountdate': taken,
            'time_to_own': tto, 'time_to_resolve': ttr, 'status': str(status), 'priority': str(priority), 'type': str(type),
            'requesttypes_id': str(request), 'is_deleted': '0', 'slas_id_tto': str(sla) if tto else '0',
            'slas_id_ttr': str(sla), 'begin_waiting_date': None}


def log(id, ticket, moment, option, old, new):
    return {'id': str(id), 'items_id': str(ticket), 'date_mod': moment, 'id_search_option': str(option), 'old_value': old, 'new_value': new}


SLAS = [{'id': '1', 'name': 'P1 4 h', 'number_time': '4', 'definition_time': 'hour', 'end_of_working_day': '0', 'calendars_id': '9'},
        {'id': '2', 'name': 'Zabbix 1 h', 'number_time': '1', 'definition_time': 'hour', 'end_of_working_day': '0', 'calendars_id': '10'}]


def test_cohort_boundaries_and_sla_ratios(db_session):
    holidays = seeded(db_session)
    snapshot = GlpiSnapshot(tickets=[
        ticket(1, '2026-09-21 23:59:00', '2026-10-01 10:00:00', ttr='2026-10-02 10:00:00'),
        ticket(2, '2026-09-22 00:00:00', '2026-10-21 23:59:59', ttr='2026-10-22 08:00:00', tto='2026-09-22 08:00:00', taken='2026-09-22 07:30:00'),
        ticket(3, '2026-10-01 08:00:00', '2026-10-22 00:00:00', ttr='2026-10-01 12:00:00'),
        ticket(4, '2026-10-01 08:00:00', '2026-10-05 08:00:00', ttr='2026-10-01 12:00:00', type=2, entity=7),
        ticket(5, '2026-10-01 08:00:00', '2026-10-02 08:00:00', sla=0, priority=4),
        ticket(6, '2026-10-01 08:00:00', '2026-10-02 08:00:00', priority=6),
        ticket(7, '2026-10-02 08:00:00', '2026-10-02 09:00:00', priority=2, ttr='2026-10-02 18:00:00', tto='2026-10-02 09:00:00'),
        ticket(8, '2026-10-02 08:00:00', '2026-10-02 09:00:00', priority=2, ttr='2026-10-02 18:00:00'),
        ticket(9, '2026-10-02 08:00:00', '2026-10-02 09:00:00', priority=2, entity=7, ttr='2026-10-02 18:00:00'),
        ticket(13, '2026-10-02 08:00:00', '2026-10-02 09:00:00', priority=2, entity=7, ttr='2026-10-02 18:00:00'),
    ], logs=[], slas=SLAS, satisfactions=[{'tickets_id': '2', 'date_answered': '2026-10-22 09:00:00', 'satisfaction': '5'}],
        knowledge_links={2}, cut=datetime(2026, 10, 22))
    result = evaluate(snapshot, OCT, holidays, {4: 'item01', 7: 'item02'})
    general = result['scopes']['geral']
    assert result['diagnostics']['excluded_before_contract'] == 1
    assert result['diagnostics']['solved_in_period'] == 8
    assert result['diagnostics']['invalid_priority'] == [6]
    assert (general['INS03']['P1']['numerator'], general['INS03']['P1']['denominator']) == (1, 1)
    assert (general['INS04']['P1']['numerator'], general['INS04']['P1']['denominator']) == (0, 1)
    assert general['INS04']['P1']['late'] == [4]
    assert general['INS02']['P1']['numerator'] == 1
    assert general['INS02']['P2']['without_sla'] == 1 and general['INS02']['P2']['denominator'] == 0
    assert result['scopes']['item01']['INS04']['P1']['denominator'] == 0
    assert result['scopes']['item02']['INS04']['P1']['denominator'] == 1
    assert (general['INS01']['numerator'], general['INS01']['denominator']) == (1, 1)
    assert (general['INS06']['numerator'], general['INS06']['denominator']) == (1, 8)
    assert (general['INS02']['P4']['numerator'], general['INS02']['P4']['denominator'], general['INS02']['P4']['without_event']) == (1, 1, 1)
    assert result['scopes']['item01']['P1P2']['excess'] == 0 and result['scopes']['item02']['P1P2']['excess'] == 0


def test_zabbix_runs_24h_and_trs45_only_when_solved(db_session):
    holidays = seeded(db_session)
    tickets = [
        ticket(10, '2026-10-10 00:00:00', '2026-10-10 04:00:00', sla=2, request=19, entity=7, priority=3),
        ticket(11, '2026-10-10 00:00:00', '2026-10-10 04:00:00', sla=2, entity=7, priority=3),
        ticket(12, '2026-10-10 00:00:00', '2026-10-10 04:00:00', sla=2, entity=7, priority=3),
        ticket(14, '2026-10-15 08:00:00', '2026-11-03 10:00:00', sla=2, entity=7, priority=3),
    ]
    october = evaluate(GlpiSnapshot(tickets=tickets, logs=[], slas=SLAS, zabbix_requesters={12}, cut=datetime(2026, 10, 22)),
                       OCT, holidays, {7: 'item02'})
    assert sorted(t['ticket'] for t in october['trs45']) == [10, 12]
    assert all(t['scope'] == 'item02' and t['state'] == 'solucionado' for t in october['trs45'])
    november = evaluate(GlpiSnapshot(tickets=tickets, logs=[], slas=SLAS, zabbix_requesters={12}, cut=datetime(2026, 11, 22)),
                        Competence.parse('2026-11'), holidays, {7: 'item02'})
    assert [t['ticket'] for t in november['trs45']] == [14]


def test_high_priority_limit_moves_last_opened_to_p3(db_session):
    holidays = seeded(db_session)
    tickets = [ticket(30 + i, f'2026-10-0{i + 1} 08:00:00', f'2026-10-0{i + 1} 13:00:00', priority=4, ttr=f'2026-10-0{i + 1} 12:00:00')
               for i in range(4)]
    tickets.append(ticket(40, '2026-10-01 08:00:00', '2026-10-01 09:00:00', priority=2, ttr='2026-10-01 18:00:00'))
    result = evaluate(GlpiSnapshot(tickets=tickets, logs=[], slas=SLAS, cut=datetime(2026, 10, 22)), OCT, holidays, {4: 'item01'})
    central = result['scopes']['item01']
    assert (central['P1P2']['high'], central['P1P2']['allowed'], central['P1P2']['excess']) == (4, 3, 1)
    assert (central['INS03']['P2']['numerator'], central['INS03']['P2']['denominator']) == (0, 3)
    assert (central['INS03']['P3']['numerator'], central['INS03']['P3']['denominator']) == (1, 1)
    assert central['INS03']['P2']['late'] == [30, 31, 32]


def test_reopening_window_skips_holiday(db_session):
    holidays = seeded(db_session)
    tickets = [ticket(20, '2026-10-01 08:00:00', '2026-10-16 15:00:00', ttr='2026-10-20 08:00:00'),
               ticket(21, '2026-10-01 08:00:00', '2026-10-19 15:00:00', ttr='2026-10-20 08:00:00')]
    logs = [log(1, 20, '2026-10-09 10:00:00', 17, '', '2026-10-09 10:00:00'),
            log(2, 20, '2026-10-16 09:00:00', 12, '5', '2'),
            log(3, 21, '2026-10-09 10:00:00', 17, '', '2026-10-09 10:00:00'),
            log(4, 21, '2026-10-16 11:00:00', 12, '5', '2')]
    result = evaluate(GlpiSnapshot(tickets=tickets, logs=logs, slas=SLAS, cut=datetime(2026, 10, 22)), OCT, holidays, {4: 'item01'})
    assert [e['ticket'] for e in result['reopen_events']] == [20]
    assert result['scopes']['geral']['INS05']['concluded'] == 2


def test_ins11_counts_profile_days_and_ignores_cover(db_session):
    seeded(db_session)
    raw = ('data;equipe;perfil;pessoa;coberta;observacao\n'
           '01/10/2026;1;operador_teleatendimento;M1;não;\n'
           '01/10/2026;1;operador_teleatendimento;M2;não;\n'
           '01/10/2026;3;gerente_servicos;M3;não;\n'
           '02/10/2026;2;tecnico_central_senior;M4;sim;substituído\n'
           '03/10/2026;1;operador_teleatendimento;M1;não;sábado\n').encode()
    assert import_absences(db_session, raw, OCT, 'auditor') == 5
    assert ins11(db_session, OCT, 'item01')['state'] == 'no_data'
    confirm_registry(db_session, OCT, 'ausencias_item01', 'auditor')
    result = ins11(db_session, OCT, 'item01')
    assert (result['minimum_staff'], result['business_days'], result['deficit_days']) == (13, 21, 2)
    assert result['value'] == (1 - Decimal(2) / (Decimal(13) * 21)) * 100
    assert round((1 - Decimal(10) / (13 * 5)) * 100, 2) == Decimal('84.62') and round((1 - Decimal(10) / (31 * 5)) * 100, 2) == Decimal('93.55')
    with pytest.raises(ValueError, match='Tabela 01'):
        import_absences(db_session, b'data;equipe;perfil;pessoa;coberta;observacao\n01/10/2026;1;gerente_servicos;M1;n\xc3\xa3o;\n', OCT, 'a')
    with pytest.raises(ValueError, match='CPF'):
        import_absences(db_session, b'data;equipe;perfil;pessoa;coberta;observacao\n01/10/2026;1;operador_teleatendimento;123.456.789-01;sim;\n', OCT, 'a')


def test_turnover_in_force_from_third_month(db_session):
    assert turnover(db_session, OCT, 'INS10')['state'] == 'not_in_force'
    december = Competence.parse('2026-12')
    raw = ('pessoa;equipe;perfil;inicio;fim;desligamento\n'
           'A;1;operador_teleatendimento;22/09/2026;;\n'
           'B;1;operador_teleatendimento;22/09/2026;10/11/2026;sem_justa_causa\n'
           'C;2;tecnico_central_senior;22/09/2026;15/10/2026;pedido_demissao\n'
           'D;5;tecnico_eletricista;22/09/2026;01/12/2026;sem_justa_causa\n').encode()
    import_staff(db_session, raw, 'auditor')
    assert turnover(db_session, december, 'INS10')['state'] == 'no_data'
    confirm_registry(db_session, december, 'pessoal', 'auditor')
    central = turnover(db_session, december, 'INS10')
    assert (central['numerator'], central['denominator']) == (1, Decimal(91 + 50 + 24) / 91)
    infra = turnover(db_session, december, 'INS09')
    assert (infra['numerator'], infra['denominator']) == (1, Decimal(71) / 91)


def row(code, scope, points, state='measured'):
    return {'code': code, 'scope': scope, 'state': state,
            'points': {'proporcional': Decimal(points), 'truncado': Decimal(points), 'pending': None}}


def test_invoice_map_assigns_each_indicator_once_and_caps():
    rows = [row('INS12', 'item01', 100), row('INS01', 'item01', 7), row('INS01', 'item02', 3), row('INS01', 'geral', 10),
            row('INS02', 'item01', 50), row('INS02', 'item02', 20), row('INS02', 'geral', 70), row('IND01', 'item02', 400),
            row('INS09', 'item02', 0, 'not_in_force')]
    trs = [{'scope': 'item01'}, {'scope': 'item02'}, {'scope': 'item02'}, {'scope': 'nao_atribuido'}]
    mapping = {'INS12': 'item01', 'INS01': 'separado', 'INS02': 'a_confirmar', 'IND01': 'item02', 'INS09': 'item02'}
    result = totals(rows, trs, mapping, OCT)
    assert result['item01']['proporcional']['points'] == Decimal(107) + Decimal(5) * Decimal('0.5')
    assert result['item02']['proporcional']['applied'] == 30 and result['item02']['proporcional']['excess'] > 0
    assert result['item02']['without_result'] == 0
    assert result['nao_atribuido']['codes'] == ['INS02'] and result['nao_atribuido']['item01']['proporcional'] == 50
    assert result['trs45_nao_atribuido']['occurrences'] == 1
    mapping['INS02'] = 'item01_geral'
    assert totals(rows, trs, mapping, OCT)['item01']['proporcional']['points'] == Decimal(177) + Decimal('2.5')


def test_seed_is_idempotent_and_keeps_manual_choice(db_session):
    seed_contract_defaults(db_session)
    db_session.get(ContractInvoiceMap, 'INS02').invoice = 'item01'
    db_session.commit()
    seed_contract_defaults(db_session)
    assert db_session.scalar(select(func.count()).select_from(ContractInvoiceMap)) == len(DEFAULT_INVOICE_MAP)
    assert db_session.get(ContractInvoiceMap, 'INS02').invoice == 'item01'
    assert {r.entity_id: r.invoice for r in db_session.scalars(select(ContractEntityMap))} == {4: 'item01', 7: 'item02', 8: 'item02'}
    assert {r.indicator for r in db_session.scalars(select(ContractInvoiceMap)) if r.confirmed} == set(CONFIRMED_SOURCES)


def test_seed_upgrades_untouched_proposal(db_session):
    db_session.add(ContractInvoiceMap(indicator='INS03', invoice='a_confirmar', confirmed=False, source=PROPOSAL))
    db_session.add(ContractInvoiceMap(indicator='INS04', invoice='item02', confirmed=False, source='Escolha manual'))
    db_session.commit()
    seed_contract_defaults(db_session)
    upgraded, manual = db_session.get(ContractInvoiceMap, 'INS03'), db_session.get(ContractInvoiceMap, 'INS04')
    assert (upgraded.invoice, upgraded.confirmed) == ('separado', True) and upgraded.source.startswith('Resposta da contratada')
    assert (manual.invoice, manual.confirmed) == ('item02', False)


def test_project_quality_by_item(db_session):
    raw = 'projeto;item;prazo;qualidade;comunicacao\nRede;02;5;5;5\nPortal;01;5;4;3\n'.encode()
    assert import_projects(db_session, raw, OCT, 'auditor') == 2
    confirm_registry(db_session, OCT, 'projetos', 'auditor')
    assert project_quality(db_session, OCT, 'item02')['value'] == 100
    assert project_quality(db_session, OCT, 'item01')['value'] == Decimal(12) / 15 * 100
    assert project_quality(db_session, OCT)['projects'] == 2
    with pytest.raises(ValueError, match='item deve ser'):
        import_projects(db_session, 'projeto;item;prazo;qualidade;comunicacao\nX;3;5;5;5\n'.encode(), OCT, 'auditor')


def test_build_without_glpi_reports_reason_not_zero(db_session, monkeypatch):
    seed_contract_defaults(db_session)
    monkeypatch.setattr('app.contract_apuracao.load_glpi', lambda *a: (None, [], 'Conector GLPI não configurado.'))
    result = build(db_session, OCT, 'DETRAN-GO', datetime(2026, 10, 22))
    ins03 = [r for r in result['rows'] if r['code'] == 'INS03']
    assert ins03 and all(r['state'] == 'no_data' and r['points'] is None for r in ins03)
    assert any('Conector GLPI' in text for _, text in result['pending'])
    assert result['totals']['item01']['proporcional']['points'] == 0


def test_cut_must_be_inside_competence():
    now = datetime(2026, 10, 17, 23, 12, 45)
    assert parse_cut('', OCT, now) == datetime(2026, 10, 17, 23, 12)
    assert parse_cut('', OCT, datetime(2026, 12, 1)) == datetime(2026, 10, 22)
    assert parse_cut('2026-10-10T08:30', OCT, now) == datetime(2026, 10, 10, 8, 30)
    for bad in ('2026-10-22T00:01', '2026-09-22T00:00', 'ontem', '2026-10-10T08:30+00:00'):
        with pytest.raises(ValueError):
            parse_cut(bad, OCT, now)


def test_api_keeps_only_daily_aggregate(db_session):
    comunix.save_config(db_session, {'user': 'api_x', 'password': 'segredo-teste', 'login_url': 'https://login.example/x',
                                     'query_url': 'https://query.example/y', 'project': 'DET_GO'}, 'auditor')
    calls = [{'data_hora': '2026/10/16 07:37:06.000', 'servico': 'DET_GO - 154', 'tempo_espera': '12', 'telefone': '62999990000',
              'agente': 'Fulano', 'conta_agente': '77', 'cti': 'c', 'uniqueid': 'u1'},
             {'data_hora': '2026/10/16 08:00:00.000', 'servico': 'DET_GO - 154', 'tempo_espera': '31', 'telefone': '62988880000'},
             {'data_hora': '2026/10/23 08:00:00.000', 'servico': 'DET_GO - 154', 'tempo_espera': '1', 'telefone': '62977770000'}]
    sent = []

    def post(url, body, headers, timeout):
        sent.append((url, headers))
        if 'login' in url:
            return b'<r><SessionID>tok</SessionID></r>'
        return json.dumps({'payload': {comunix.RESULT_KEY: calls}}).encode()

    assert comunix.fetch(db_session, OCT.start, OCT.end, 'DETRAN-GO', 'auditor', post=post) == {'calls': 3, 'groups': 1}
    assert sent[0][1] == {} and sent[1][1] == {'Authorization': 'Bearer tok', 'Accept': 'application/json'}
    assert comunix._session_id(b'{"r": {"SessionID": "abc"}}') == 'abc' and comunix._session_id(b'<html>') is None
    stored = db_session.scalars(select(UraApiDaily)).all()
    assert [(r.day, r.answered, r.answered_30) for r in stored] == [(date(2026, 10, 16), 2, 1)]
    dumped = repr([vars(r) for r in stored])
    assert '6299' not in dumped and 'Fulano' not in dumped and 'u1' not in dumped
    assert 'segredo-teste' not in repr(db_session.execute(select(comunix.AdminActionLog.message)).all())
    stored_password = comunix.get_system_config_value(db_session, comunix.KEYS['password'])
    assert stored_password.startswith('gAAAAA') and comunix.load_config(db_session)['password'] == 'segredo-teste'
    with pytest.raises(ValueError, match='https'):
        comunix.save_config(db_session, {'user': 'x', 'login_url': 'http://a', 'query_url': 'https://b', 'project': 'p'}, 'auditor')


def test_ura_replacement_requires_reason_and_keeps_previous_version(db_session):
    first = import_ura_csv(db_session, sample_csv(), project='DETRAN-GO', source_name='a.csv', username='auditor')
    changed = sample_csv(answered=9)
    with pytest.raises(ValueError, match='Conflito'):
        import_ura_csv(db_session, changed, project='DETRAN-GO', source_name='b.csv', username='auditor')
    with pytest.raises(ValueError):
        import_ura_csv(db_session, changed, project='DETRAN-GO', source_name='b.csv', username='auditor', replace=True, reason='curta')
    run = import_ura_csv(db_session, changed, project='DETRAN-GO', source_name='b.csv', username='auditor', replace=True,
                         reason='Reenvio corrigido pela contratada')
    assert run.replaced_count == 1
    daily = db_session.scalar(select(UraDaily))
    assert daily.answered == 9 and daily.import_id == run.id
    revision = db_session.scalar(select(UraDailyRevision))
    assert revision.previous_import_id == first.id and json.loads(revision.before)['answered'] == 10


def test_computed_maximum_goal_in_months_3_and_4():
    from app.billing_rules import adjusted_goal
    from app.metrics import measure
    december = Competence.parse('2026-12')
    goal = adjusted_goal('15', 'maximum', december)
    assert measure(1, 10, goal=goal, direction='maximum')['meets_goal'] is True
    assert indicator_points('INS05', Decimal(20), adjusted_goal('8', 'maximum', december), 'maximum', december)['proporcional'] > 0
    with pytest.raises(ValueError):
        measure(1, 10, goal='0.0000000000001')


def test_dashboard_competence_ratio_sources_and_empty_gauge(monkeypatch):
    from types import SimpleNamespace
    from app.routes import dashboard
    monkeypatch.setattr(dashboard, 'current_competence', lambda: OCT)
    assert dashboard.dashboard_dates(None, None) == (date(2026, 9, 22), date(2026, 10, 21))
    with pytest.raises(ValueError):
        dashboard.dashboard_dates(None, None, '2026-09')
    assert dashboard.competence_label(date(2026, 9, 22), date(2026, 10, 21)) == '2026-10'
    assert dashboard.competence_label(date(2026, 10, 1), date(2026, 10, 21)) == ''
    counts = {'atendidas': 767, 'recebidas': 779}
    monkeypatch.setattr(dashboard, 'get_source_count', lambda _s, source, *_: counts[source.name])
    widget = SimpleNamespace(gauge_mode='ratio_sources', goal_direction='minimum', meta_gauge=95, source_id_b=2,
                             source=SimpleNamespace(name='atendidas'), source_b=SimpleNamespace(name='recebidas'))
    result = {}
    dashboard.apply_widget_gauge(result, widget, 'day', OCT.start, OCT.end)
    assert result['metric']['value'] == Decimal(767) / 779 * 100 and result['status'] == 'ok'
    widget.source_b = None
    with pytest.raises(ValueError, match='fonte B'):
        dashboard.apply_widget_gauge({}, widget, 'day', OCT.start, OCT.end)
    monkeypatch.setattr(dashboard, 'execute_gauge_values', lambda *_: (None,))
    empty = {}
    dashboard.apply_widget_gauge(empty, SimpleNamespace(gauge_mode='value', goal_direction='minimum', meta_gauge=95), 'day', OCT.start, OCT.end)
    assert empty['status'] == 'sem_amostra' and empty['percentual'] is None


def test_pending_status_pauses_trs45_clock(db_session):
    holidays = seeded(db_session)
    tickets = [ticket(30, '2026-10-05 08:00:00', '2026-10-07 10:00:00', ttr='2026-10-05 12:00:00'),
               ticket(31, '2026-10-05 08:00:00', '2026-10-07 10:00:00', ttr='2026-10-05 12:00:00'),
               ticket(32, '2026-10-05 08:00:00', status=2, ttr='2026-10-05 12:00:00')]
    logs = [log(1, 30, '2026-10-05 09:00:00', 12, '2', '4'), log(2, 30, '2026-10-07 09:00:00', 12, '4', '2')]
    result = evaluate(GlpiSnapshot(tickets=tickets, logs=logs, slas=SLAS, cut=datetime(2026, 10, 7, 12)), OCT, holidays, {4: 'item01'})
    assert [t['ticket'] for t in result['trs45']] == [31]
    assert result['trs45'][0]['net_seconds'] == 26 * 3600


def test_final_category_without_resolution_sla_leaves_trs45_ins03_and_ins04(db_session):
    holidays = seeded(db_session)
    tickets = [ticket(50, '2026-10-05 08:00:00', '2026-10-07 10:00:00', ttr='2026-10-05 12:00:00', priority=3),
               ticket(51, '2026-10-05 08:00:00', '2026-10-07 10:00:00', ttr='2026-10-05 12:00:00', priority=3),
               ticket(52, '2026-10-05 08:00:00', '2026-10-07 10:00:00', ttr='2026-10-05 12:00:00', priority=3, type=2)]
    for row, category in zip(tickets, ('1515', '1400', '1637')):
        row['itilcategories_id'] = category
    snapshot = GlpiSnapshot(tickets=tickets, logs=[], slas=SLAS, cut=datetime(2026, 10, 7, 12),
                            categories={1515: 'CAT-DTGO-0032', 1400: 'CAT-DTGO-0001', 1637: 'cat-dtgo-0356'})
    result = evaluate(snapshot, OCT, holidays, {4: 'item01'})
    assert [t['ticket'] for t in result['trs45']] == [51]
    ins03, ins04 = result['scopes']['item01']['INS03']['P3'], result['scopes']['item01']['INS04']['P3']
    assert (ins03['denominator'], ins03['no_resolution_category'], ins03['without_sla'], ins03['late']) == (1, 1, 0, [51])
    assert (ins04['denominator'], ins04['no_resolution_category'], ins04['state']) == (0, 1, 'no_sample')
    assert result['diagnostics']['category_without_resolution_sla'] == [50, 52]


def test_reopening_after_competence_end_counts_in_the_concluding_competence(db_session):
    holidays = seeded(db_session)
    tickets = [ticket(40, '2026-10-01 08:00:00', '2026-10-27 15:00:00', ttr='2026-10-28 08:00:00')]
    logs = [log(1, 40, '2026-10-20 10:00:00', 17, '', '2026-10-20 10:00:00'),
            log(2, 40, '2026-10-23 09:00:00', 12, '5', '2')]
    early = evaluate(GlpiSnapshot(tickets=tickets, logs=logs[:1], slas=SLAS, cut=datetime(2026, 10, 22), observed=datetime(2026, 10, 22, 1)),
                     OCT, holidays, {4: 'item01'})
    assert early['reopen_provisional'] and early['reopen_window_end'] == datetime(2026, 10, 27, 23, 59, 59)
    late = evaluate(GlpiSnapshot(tickets=tickets, logs=logs, slas=SLAS, cut=datetime(2026, 10, 22), observed=datetime(2026, 10, 30)),
                    OCT, holidays, {4: 'item01'})
    assert [e['ticket'] for e in late['reopen_events']] == [40] and not late['reopen_provisional']
    november = evaluate(GlpiSnapshot(tickets=tickets, logs=logs, slas=SLAS, cut=datetime(2026, 11, 22)), Competence.parse('2026-11'),
                        holidays, {4: 'item01'})
    assert november['reopen_events'] == []


def test_availability_rejects_unavailable_above_total(db_session):
    from app.contract_registries import import_availability
    header = 'indicador;ativo;htp;hip;hmp;hit\n'
    with pytest.raises(ValueError, match='HIP'):
        import_availability(db_session, (header + 'IND01;firewall;10;500;0;0\n').encode(), OCT, 'a')
    with pytest.raises(ValueError, match='HMP'):
        import_availability(db_session, (header + 'IND01;firewall;10;2;2;1\n').encode(), OCT, 'a')
    assert import_availability(db_session, (header + 'IND01;firewall;720;3;1;1\n').encode(), OCT, 'a') == 1


def test_separate_invoice_needs_per_invoice_result():
    rows = [row('INS08', 'geral', 100), row('INS11', 'item01', 10), row('INS11', 'item02', 20), row('INS11', 'geral', 30)]
    result = totals(rows, [], {'INS08': 'separado', 'INS11': 'separado'}, OCT)
    assert result['nao_atribuido']['codes'] == ['INS08'] and result['nao_atribuido']['geral']['proporcional'] == 100
    assert (result['item01']['proporcional']['points'], result['item02']['proporcional']['points']) == (10, 20)
