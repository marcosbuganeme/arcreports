"""Indicadores de cadastro: INS08 (projetos), INS09/INS10 (rotatividade), INS11 (disponibilidade de
profissionais) e IND01/IND02 (disponibilidade de ativos). Sem cadastro confirmado não há resultado."""
import csv
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from io import StringIO

from sqlalchemy import delete, select

from app.billing_rules import Competence, adjusted_goal, staffing_window
from app.business_calendar import business_days, holiday_set
from app.metrics import measure
from app.models import (AdminActionLog, ContractAbsence, ContractAvailability, ContractProjectScore,
                        ContractRegistryStatus, ContractStaff)


@dataclass(frozen=True)
class Profile:
    team: int
    code: str
    label: str
    minimum: int


PROFILES = (
    Profile(1, 'operador_teleatendimento', 'Operador de Teleatendimento', 4),
    Profile(2, 'tecnico_central_senior', 'Técnico de Suporte de Central de Serviços de TIC (Sênior)', 4),
    Profile(3, 'gerente_servicos', 'Gerente de Serviços de TIC', 1),
    Profile(3, 'analista_itil', 'Analista de Sistemas da Informação Sênior - Processos ITIL', 1),
    Profile(3, 'administrador_itsm', 'Administrador de Sistemas Computacionais Sênior - ITSM', 1),
    Profile(3, 'supervisor_central', 'Supervisor de Central de Atendimento', 1),
    Profile(3, 'gerente_projetos_central', 'Gerente de Projetos - Central de Serviços', 1),
    Profile(4, 'operador_monitoramento', 'Operador de Monitoramento (NOC)', 4),
    Profile(5, 'administrador_rede_pleno', 'Administrador de Infraestrutura de Rede Pleno', 1),
    Profile(5, 'administrador_rede_senior', 'Administrador de Infraestrutura de Rede Sênior', 1),
    Profile(5, 'tecnico_infra_pleno', 'Técnico de Suporte de Infraestrutura de TIC (Pleno)', 1),
    Profile(5, 'tecnico_infra_senior', 'Técnico de Suporte de Infraestrutura de TIC (Sênior)', 2),
    Profile(5, 'tecnico_eletricista', 'Técnico Eletricista', 1),
    Profile(6, 'administrador_seguranca_senior', 'Administrador de Sistemas de Segurança da Informação Sênior', 1),
    Profile(7, 'administrador_ciberseguranca_pleno', 'Administrador de Sistemas de Segurança da Informação Pleno - Cibersegurança', 1),
    Profile(8, 'administrador_linux_pleno', 'Administrador de Sistemas Computacionais Pleno - Linux/Unix', 1),
    Profile(8, 'administrador_linux_senior', 'Administrador de Sistemas Computacionais Sênior - Linux/Unix', 2),
    Profile(9, 'administrador_windows_senior', 'Administrador de Sistemas Computacionais Sênior - Windows', 1),
    Profile(9, 'tecnico_infra_senior', 'Técnico de Suporte de Infraestrutura de TIC (Sênior)', 2),
    Profile(10, 'administrador_banco_senior', 'Administrador de Banco de Dados Sênior', 1),
    Profile(11, 'administrador_dados_pleno', 'Administrador de Dados Pleno', 1),
    Profile(12, 'gerente_infraestrutura', 'Gerente de Infraestrutura de TIC', 1),
    Profile(12, 'gerente_projetos_infra', 'Gerente de Projetos - Infraestrutura de TIC', 1),
)
PROFILE_KEYS = {(p.team, p.code): p for p in PROFILES}
CENTRAL_TEAMS = frozenset({1, 2, 3})
INFRA_TEAMS = frozenset(range(5, 13))
INS11_SCOPES = {'item01': CENTRAL_TEAMS, 'item02': INFRA_TEAMS, 'geral': CENTRAL_TEAMS | INFRA_TEAMS}
REGISTRIES = {
    'ausencias_item01': 'Ausências das equipes 1 a 3 (Central de Serviços)',
    'ausencias_item02': 'Ausências das equipes 5 a 12 (Infraestrutura de TIC)',
    'pessoal': 'Cadastro de alocações e desligamentos',
    'projetos': 'Notas dos projetos e demandas planejadas entregues',
    'disponibilidade': 'Disponibilidade dos ativos de criticidade "Alta"',
}
DEPARTURES = {'sem_justa_causa', 'justa_causa', 'pedido_demissao', 'termino_contrato', 'transferencia'}
PROJECT_ITEMS = {'01': 'item01', '1': 'item01', 'item01': 'item01', '02': 'item02', '2': 'item02', 'item02': 'item02'}
YES = {'sim', 's', 'true', '1', 'x'}
NO = {'nao', 'não', 'n', 'false', '0', ''}


def minimum(teams):
    return sum(p.minimum for p in PROFILES if p.team in teams)


def confirmed(db, competence: Competence, registry):
    return db.scalar(select(ContractRegistryStatus).where(ContractRegistryStatus.competence == competence.label,
                                                          ContractRegistryStatus.registry == registry))


def confirm_registry(db, competence: Competence, registry, username):
    if registry not in REGISTRIES:
        raise ValueError('Cadastro inválido.')
    if not confirmed(db, competence, registry):
        db.add(ContractRegistryStatus(competence=competence.label, registry=registry, confirmed_by=username))
    db.add(AdminActionLog(username=username, action='confirm_contract_registry', table_name='contract_registry_status',
                          status='success', message=f'Cadastro {registry} confirmado como completo para {competence.label}.'))
    db.commit()


def reopen_registry(db, competence: Competence, registry, username):
    db.execute(delete(ContractRegistryStatus).where(ContractRegistryStatus.competence == competence.label,
                                                    ContractRegistryStatus.registry == registry))
    db.add(AdminActionLog(username=username, action='reopen_contract_registry', table_name='contract_registry_status',
                          status='success', message=f'Cadastro {registry} reaberto para {competence.label}.'))
    db.commit()


def ins11(db, period: Competence, scope):
    teams = INS11_SCOPES[scope]
    registries = ['ausencias_item01', 'ausencias_item02'] if scope == 'geral' else ['ausencias_' + scope]
    holidays = holiday_set(db, period.start, period.end)
    days = business_days(period.start, period.end, holidays)
    goal = adjusted_goal('98', 'minimum', period)
    n_minimum = minimum(teams)
    base = {'code': 'INS11', 'scope': scope, 'business_days': len(days), 'minimum_staff': n_minimum,
            'formula': '(1 − ΣDi ÷ (N × D)) × 100', 'direction': 'minimum', 'base_goal': Decimal(98)}
    if not all(confirmed(db, period, r) for r in registries):
        return {**base, 'state': 'no_data', 'value': None, 'goal': goal, 'deficit_days': None, 'profiles': [],
                'missing': [REGISTRIES[r] for r in registries if not confirmed(db, period, r)]}
    business = set(days)
    rows = db.scalars(select(ContractAbsence).where(ContractAbsence.day >= period.start, ContractAbsence.day <= period.end,
                                                    ContractAbsence.team.in_(teams))).all()
    deficit = {}
    for row in rows:
        if row.covered or row.day not in business:
            continue
        deficit.setdefault((row.team, row.profile), set()).add(row.day)
    total = sum(len(v) for v in deficit.values())
    value = (1 - Decimal(total) / (Decimal(n_minimum) * Decimal(len(days)))) * 100 if days else None
    metric = measure(value, mode='value', goal=goal, direction='minimum') if value is not None else measure(None, mode='value', goal=goal)
    profiles = [{'team': team, 'profile': PROFILE_KEYS[(team, code)].label if (team, code) in PROFILE_KEYS else code,
                 'days': sorted(d.isoformat() for d in days_)} for (team, code), days_ in sorted(deficit.items())]
    return {**base, **metric, 'deficit_days': total, 'profiles': profiles, 'missing': []}


def turnover(db, period: Competence, code):
    teams = CENTRAL_TEAMS if code == 'INS10' else INFRA_TEAMS
    window = staffing_window(period)
    goal = adjusted_goal('15', 'maximum', period)
    base = {'code': code, 'scope': 'item01' if code == 'INS10' else 'item02', 'direction': 'maximum', 'base_goal': Decimal(15),
            'teams': sorted(teams), 'window': window,
            'formula': ('desligados sem justa causa ÷ média do quadro das equipes ' + ('01 a 03' if code == 'INS10' else '05 a 12')
                        + ' nas três competências × 100'),
            'basis': 'Denominador: média diária do quadro alocado nas três competências (resposta de 18/10/2026)'}
    if window is None:
        return {**base, 'state': 'not_in_force', 'value': None, 'goal': goal, 'numerator': None, 'denominator': None, 'missing': []}
    if not confirmed(db, period, 'pessoal'):
        return {**base, 'state': 'no_data', 'value': None, 'goal': goal, 'numerator': None, 'denominator': None,
                'missing': [REGISTRIES['pessoal']]}
    start, end = window
    rows = db.scalars(select(ContractStaff).where(ContractStaff.team.in_(teams), ContractStaff.start_date <= end)).all()
    days = (end - start).days + 1
    person_days = sum((min(r.end_date or end, end) - max(r.start_date, start)).days + 1 for r in rows
                      if r.end_date is None or r.end_date >= start)
    average = Decimal(person_days) / days
    departed = {r.person_ref for r in rows if r.end_date and start <= r.end_date <= end and r.departure == 'sem_justa_causa'}
    metric = measure(len(departed), average, goal=goal, direction='maximum')
    return {**base, **metric, 'average_staff': average, 'missing': []}


def project_quality(db, period: Competence, scope='geral'):
    goal = adjusted_goal('80', 'minimum', period)
    base = {'code': 'INS08', 'scope': scope, 'direction': 'minimum', 'base_goal': Decimal(80),
            'formula': 'Σ notas ÷ (projetos × 3 critérios × 5) × 100'}
    if not confirmed(db, period, 'projetos'):
        return {**base, 'state': 'no_data', 'value': None, 'goal': goal, 'numerator': None, 'denominator': None,
                'missing': [REGISTRIES['projetos']]}
    query = select(ContractProjectScore).where(ContractProjectScore.competence == period.label)
    if scope != 'geral':
        query = query.where(ContractProjectScore.invoice == scope)
    rows = db.scalars(query).all()
    obtained = sum(r.deadline_score + r.quality_score + r.communication_score for r in rows)
    metric = measure(obtained, len(rows) * 15, goal=goal, direction='minimum')
    return {**base, **metric, 'projects': len(rows), 'missing': []}


def availability(db, period: Competence, code):
    base_goal = Decimal('99.4') if code == 'IND01' else Decimal('98.5')
    goal = adjusted_goal(base_goal, 'minimum', period)
    base = {'code': code, 'scope': 'item02', 'direction': 'minimum', 'base_goal': base_goal,
            'formula': 'média dos ativos de (HTP − HIP) ÷ HTP × 100; HIP sem manutenção programada e sem evento de terceiro'}
    if not confirmed(db, period, 'disponibilidade'):
        return {**base, 'state': 'no_data', 'value': None, 'goal': goal, 'assets': 0, 'missing': [REGISTRIES['disponibilidade']]}
    rows = db.scalars(select(ContractAvailability).where(ContractAvailability.competence == period.label,
                                                         ContractAvailability.indicator == code)).all()
    values = []
    for row in rows:
        total = Decimal(row.total_hours)
        counted = max(Decimal(0), Decimal(row.unavailable_hours) - Decimal(row.planned_hours) - Decimal(row.third_party_hours))
        values.append((total - counted) / total * 100)
    if not values:
        return {**base, **measure(None, mode='value', goal=goal), 'assets': 0, 'missing': []}
    metric = measure(sum(values) / len(values), mode='value', goal=goal, direction='minimum')
    return {**base, **metric, 'assets': len(values), 'missing': []}


def _reader(raw: bytes, expected):
    try:
        text = raw.decode('utf-8').lstrip('﻿')
    except UnicodeDecodeError as exc:
        raise ValueError('Salve o CSV em UTF-8 antes de importar.') from exc
    if len(raw) > 2_000_000:
        raise ValueError('O CSV deve ter no máximo 2 MB.')
    reader = csv.reader(StringIO(text, newline=''), delimiter=';', strict=True)
    try:
        header = [h.strip().lower() for h in next(reader, [])]
        if header != expected:
            raise ValueError('Cabeçalho esperado: ' + ';'.join(expected))
        for line, row in enumerate(reader, 2):
            if not row or not any(c.strip() for c in row):
                continue
            if len(row) != len(expected):
                raise ValueError(f'Linha {line}: esperadas {len(expected)} colunas.')
            yield line, [c.strip() for c in row]
    except csv.Error as exc:
        raise ValueError('CSV malformado.') from exc


def _date(value, line):
    for fmt in ('%d/%m/%Y', '%Y-%m-%d'):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    raise ValueError(f'Linha {line}: data inválida; use DD/MM/AAAA.')


def _profile(team, code, line):
    try:
        team = int(team)
    except ValueError as exc:
        raise ValueError(f'Linha {line}: equipe inválida.') from exc
    if (team, code) not in PROFILE_KEYS:
        raise ValueError(f'Linha {line}: perfil {code} não pertence à equipe {team} na Tabela 01.')
    return team


def _ref(value, line):
    if not value or len(value) > 80 or value.replace('.', '').replace('-', '').isdigit() and len(value.replace('.', '').replace('-', '')) == 11:
        raise ValueError(f'Linha {line}: informe a matrícula ou um código da pessoa (sem CPF), até 80 caracteres.')
    return value


def _score(value, line):
    if value not in {'1', '2', '3', '4', '5'}:
        raise ValueError(f'Linha {line}: nota deve ser de 1 a 5.')
    return int(value)


def _hours(value, line):
    try:
        number = Decimal(value.replace(',', '.'))
    except InvalidOperation as exc:
        raise ValueError(f'Linha {line}: horas inválidas.') from exc
    if not number.is_finite() or number < 0:
        raise ValueError(f'Linha {line}: horas inválidas.')
    return str(number)


def import_absences(db, raw, period: Competence, username):
    rows = []
    for line, (day, team, code, person, covered, note) in _reader(raw, ['data', 'equipe', 'perfil', 'pessoa', 'coberta', 'observacao']):
        day = _date(day, line)
        if not period.start <= day <= period.end:
            raise ValueError(f'Linha {line}: data fora da competência {period.label}.')
        if covered.lower() not in YES | NO:
            raise ValueError(f'Linha {line}: coberta deve ser sim ou não.')
        rows.append(ContractAbsence(day=day, team=_profile(team, code, line), profile=code, person_ref=_ref(person, line),
                                    covered=covered.lower() in YES, note=note[:255] or None, username=username))
    db.execute(delete(ContractAbsence).where(ContractAbsence.day >= period.start, ContractAbsence.day <= period.end))
    db.add_all(rows)
    db.add(AdminActionLog(username=username, action='import_contract_absences', table_name='contract_absences', status='success',
                          message=f'{len(rows)} ausências substituídas na competência {period.label}.'))
    db.commit()
    return len(rows)


def import_staff(db, raw, username):
    rows = []
    for line, (person, team, code, start, end, departure) in _reader(raw, ['pessoa', 'equipe', 'perfil', 'inicio', 'fim', 'desligamento']):
        start_date = _date(start, line)
        end_date = _date(end, line) if end else None
        if end_date and end_date < start_date:
            raise ValueError(f'Linha {line}: fim anterior ao início.')
        departure = departure.lower() or None
        if departure and departure not in DEPARTURES:
            raise ValueError(f'Linha {line}: desligamento deve ser um de ' + ', '.join(sorted(DEPARTURES)) + '.')
        if departure and not end_date:
            raise ValueError(f'Linha {line}: desligamento exige data de fim.')
        rows.append(ContractStaff(person_ref=_ref(person, line), team=_profile(team, code, line), profile=code,
                                  start_date=start_date, end_date=end_date, departure=departure, username=username))
    db.execute(delete(ContractStaff))
    db.add_all(rows)
    db.add(AdminActionLog(username=username, action='import_contract_staff', table_name='contract_staff', status='success',
                          message=f'Cadastro de pessoal substituído: {len(rows)} alocações.'))
    db.commit()
    return len(rows)


def import_projects(db, raw, period: Competence, username):
    rows = []
    for line, (project, item, deadline, quality, communication) in _reader(raw, ['projeto', 'item', 'prazo', 'qualidade', 'comunicacao']):
        if not project or len(project) > 160:
            raise ValueError(f'Linha {line}: nome do projeto inválido.')
        invoice = PROJECT_ITEMS.get(item.lower().replace(' ', ''))
        if invoice is None:
            raise ValueError(f'Linha {line}: item deve ser 01 (Central de Serviços) ou 02 (Infraestrutura de TIC).')
        rows.append(ContractProjectScore(competence=period.label, project=project, invoice=invoice, deadline_score=_score(deadline, line),
                                         quality_score=_score(quality, line), communication_score=_score(communication, line), username=username))
    db.execute(delete(ContractProjectScore).where(ContractProjectScore.competence == period.label))
    db.add_all(rows)
    db.add(AdminActionLog(username=username, action='import_contract_projects', table_name='contract_project_scores', status='success',
                          message=f'{len(rows)} projetos substituídos na competência {period.label}.'))
    db.commit()
    return len(rows)


def import_availability(db, raw, period: Competence, username):
    rows = []
    for line, (indicator, asset, total, unavailable, planned, third) in _reader(raw, ['indicador', 'ativo', 'htp', 'hip', 'hmp', 'hit']):
        if indicator.upper() not in {'IND01', 'IND02'}:
            raise ValueError(f'Linha {line}: indicador deve ser IND01 ou IND02.')
        if not asset or len(asset) > 160:
            raise ValueError(f'Linha {line}: ativo inválido.')
        total, unavailable = _hours(total, line), _hours(unavailable, line)
        planned, third = _hours(planned or '0', line), _hours(third or '0', line)
        if Decimal(total) == 0:
            raise ValueError(f'Linha {line}: HTP deve ser maior que zero.')
        if Decimal(unavailable) > Decimal(total) or Decimal(planned) + Decimal(third) > Decimal(unavailable):
            raise ValueError(f'Linha {line}: HIP não pode passar de HTP, e HMP + HIT não podem passar de HIP.')
        rows.append(ContractAvailability(competence=period.label, indicator=indicator.upper(), asset=asset, total_hours=total,
                                         unavailable_hours=unavailable, planned_hours=planned, third_party_hours=third, username=username))
    db.execute(delete(ContractAvailability).where(ContractAvailability.competence == period.label))
    db.add_all(rows)
    db.add(AdminActionLog(username=username, action='import_contract_availability', table_name='contract_availability', status='success',
                          message=f'{len(rows)} ativos substituídos na competência {period.label}.'))
    db.commit()
    return len(rows)
