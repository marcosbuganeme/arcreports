"""Apuração dos indicadores de chamados (INS01 a INS07), do TRS45 e do limite de 60% de P1+P2.

Regras fechadas com a contratada em 17/10/2026: competência 22 a 21 com o último dia inteiro;
só chamados abertos a partir de 22/09/2026 00:00; "encerrado no mês" = data de solução; prazo =
SLA gravado no GLPI e chamado sem SLA fora do denominador; prioridade GLPI 5/4/3/2 = P1/P2/P3/P4,
1 e 6 são erro de dado; relógio 24 h só para chamado do Zabbix, os demais de segunda a sexta, das
7h às 19h, sem feriados nem pontos facultativos.

Respostas de 18/10/2026: TRS45 só de chamado solucionado, uma vez, na competência da solução;
INS02 conta os iniciados no prazo e o chamado sem data de assumido conta como iniciado no prazo;
limite de 60% de P1+P2 por item, e os últimos abertos acima dele passam ao prazo de P3.
Resposta de 18/10/2026 à pergunta 11: chamado cuja categoria final está na regra 455 do GLPI
("Categorias sem SLA de Solução") sai do TRS45, do INS03 e do INS04, mesmo com o prazo de solução
que o GLPI deixou gravado; a regra casa pelo código da categoria.
"""
from __future__ import annotations
import bisect
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from app.billing_rules import CONTRACT_START, Competence, adjusted_goal, knowledge_goal
from app.business_calendar import add_business_days
from app.metrics import measure

PRIORITY = {5: 'P1', 4: 'P2', 3: 'P3', 2: 'P4'}
PRIORITIES = ('P1', 'P2', 'P3', 'P4')
ZABBIX_REQUEST_TYPE = 19
ZABBIX_REQUESTER = 1062
CALENDARS_24X7 = (0, 10)
INCIDENT, REQUEST = 1, 2
PENDING_STATUS = 4
PAUSED_STATUSES = (4, 5, 6)
SOLVED_STATUSES = (5, 6)
OPEN_STATUSES = (1, 2, 3, 4)
LOG_STATUS, LOG_SOLVEDATE, LOG_ENTITY = 12, 17, 80
BUSINESS_START, BUSINESS_END = time(7), time(19)
CONTRACT_OPENING = datetime.combine(CONTRACT_START, time.min)
SCOPES = ('item01', 'item02', 'geral')
REOPEN_DAYS = 4
REOPEN_LOOKAHEAD = timedelta(days=14)
HIGH_PRIORITY_LIMIT = Decimal(60)
P3_OWN_SECONDS = 3600
P3_RESOLVE_SECONDS = {INCIDENT: 6 * 3600, REQUEST: 12 * 3600}
NO_RESOLUTION_CATEGORIES = frozenset(f'CAT-DTGO-{n:04d}' for n in (27, 32, 33, 34, 35, 309, 310, 311, 312, 333, 334, 335, 336, 337,
                                                                  338, 339, 340, 341, 342, 343, 344, 356, 357, 358, 359))


def to_int(value, default=0):
    if value in (None, ''):
        return default
    try:
        return int(str(value).split('.')[0])
    except ValueError:
        return default


def to_datetime(value):
    if value in (None, '') or str(value).startswith('0000-'):
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None, microsecond=0)
    text = str(value).replace('T', ' ')[:19]
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


@dataclass
class GlpiSnapshot:
    tickets: list
    logs: list
    slas: list
    satisfactions: list = field(default_factory=list)
    knowledge_links: set = field(default_factory=set)
    item_links: set = field(default_factory=set)
    zabbix_requesters: set = field(default_factory=set)
    categories: dict = field(default_factory=dict)
    cut: datetime | None = None
    observed: datetime | None = None


class ContractClock:
    """Expediente contratual: 7h às 19h em dias úteis, ou 24 h para o Zabbix."""

    def __init__(self, holidays, low: date, high: date):
        self.starts, self.ends, self.prefix = [], [], [0]
        current = low
        while current <= high:
            if current.weekday() < 5 and current not in holidays:
                a, b = datetime.combine(current, BUSINESS_START), datetime.combine(current, BUSINESS_END)
                self.starts.append(a)
                self.ends.append(b)
                self.prefix.append(self.prefix[-1] + (b - a).total_seconds())
            current += timedelta(days=1)

    def cumulative(self, moment):
        i = bisect.bisect_right(self.ends, moment)
        total = self.prefix[i]
        if i < len(self.starts) and moment > self.starts[i]:
            total += (moment - self.starts[i]).total_seconds()
        return total

    def seconds(self, a, b, continuous=False):
        if a is None or b is None or b <= a:
            return 0
        if continuous:
            return int((b - a).total_seconds())
        return int(self.cumulative(b) - self.cumulative(a))


def sla_seconds(sla):
    if not sla or to_int(sla.get('end_of_working_day')):
        return None
    unit = {'minute': 60, 'hour': 3600}.get(str(sla.get('definition_time') or ''))
    number = to_int(sla.get('number_time'))
    return number * unit if unit and number > 0 else None


class Ticket:
    def __init__(self, row, logs, zabbix_requesters):
        self.id = to_int(row['id'])
        self.entity = to_int(row['entities_id'])
        self.opened = to_datetime(row['date'])
        self.solved = to_datetime(row.get('solvedate'))
        self.taken = to_datetime(row.get('takeintoaccountdate'))
        self.time_to_own = to_datetime(row.get('time_to_own'))
        self.time_to_resolve = to_datetime(row.get('time_to_resolve'))
        self.status = to_int(row.get('status'))
        self.raw_priority = to_int(row.get('priority'))
        self.priority = PRIORITY.get(self.raw_priority)
        self.original_priority = self.priority
        self.downgraded = False
        self.type = to_int(row.get('type'))
        self.category = to_int(row.get('itilcategories_id'))
        self.no_resolution_category = False
        self.sla_tto = to_int(row.get('slas_id_tto'))
        self.sla_ttr = to_int(row.get('slas_id_ttr'))
        self.begin_waiting = to_datetime(row.get('begin_waiting_date'))
        self.zabbix = to_int(row.get('requesttypes_id')) == ZABBIX_REQUEST_TYPE or self.id in zabbix_requesters
        ordered = sorted(logs, key=lambda e: (to_datetime(e['date_mod']) or datetime.min, to_int(e.get('id'))))
        self.status_logs = [e for e in ordered if to_int(e['id_search_option']) == LOG_STATUS]
        self.entity_logs = [e for e in ordered if to_int(e['id_search_option']) == LOG_ENTITY]
        conclusions = set()
        for e in ordered:
            if to_int(e['id_search_option']) != LOG_SOLVEDATE:
                continue
            new, old = to_datetime(e.get('new_value')), to_datetime(e.get('old_value'))
            if new:
                conclusions.add(new)
            elif old:
                conclusions.add(old)
        if self.solved:
            conclusions.add(self.solved)
        self.conclusions = sorted(conclusions)

    def reopenings(self):
        for e in self.status_logs:
            if to_int(e.get('old_value')) in SOLVED_STATUSES and to_int(e.get('new_value')) in OPEN_STATUSES:
                yield to_datetime(e['date_mod'])

    def pending_seconds(self, clock, until):
        """Tempo parado pelo histórico de status: Pendente, e Solucionado/Fechado antes de uma reabertura."""
        if not self.status_logs:
            if self.status == PENDING_STATUS and self.begin_waiting:
                return clock.seconds(max(self.opened, self.begin_waiting), until, self.zabbix)
            return 0
        state = to_int(self.status_logs[0].get('old_value'))
        cursor, total = self.opened, 0
        for e in self.status_logs:
            moment = to_datetime(e['date_mod'])
            if moment is None or moment < cursor:
                continue
            if moment > until:
                break
            if state in PAUSED_STATUSES:
                total += clock.seconds(cursor, moment, self.zabbix)
            state, cursor = to_int(e.get('new_value')), moment
        if state in PAUSED_STATUSES:
            total += clock.seconds(cursor, until, self.zabbix)
        return total

    def resolve_limit(self, slas):
        if self.no_resolution_category:
            return None
        limit = sla_seconds(slas.get(self.sla_ttr)) if self.sla_ttr else None
        if limit and self.downgraded:
            return P3_RESOLVE_SECONDS.get(self.type)
        return limit

    def owned_on_time(self, clock):
        if self.taken is None:
            return True
        if self.downgraded:
            return clock.seconds(self.opened, self.taken, self.zabbix) <= P3_OWN_SECONDS
        return self.taken <= self.time_to_own

    def solved_on_time(self, clock):
        if self.downgraded:
            return self.net_seconds(clock, self.solved) <= P3_RESOLVE_SECONDS.get(self.type, 0)
        return self.solved <= self.time_to_resolve

    def net_seconds(self, clock, until):
        end = min(self.solved, until) if self.solved else until
        if end <= self.opened:
            return 0
        return max(0, clock.seconds(self.opened, end, self.zabbix) - self.pending_seconds(clock, end))


def scope_of(ticket, entity_invoice):
    return entity_invoice.get(ticket.entity, 'nao_atribuido')


def apply_high_priority_limit(solved, entity_invoice):
    """TR 8.10/8.11 por item: P1+P2 além de 60% dos solucionados, os últimos abertos, ficam com prazo de P3."""
    excess = {}
    for invoice in ('item01', 'item02'):
        valid = [t for t in solved if t.priority and scope_of(t, entity_invoice) == invoice]
        high = sorted((t for t in valid if t.priority in ('P1', 'P2')), key=lambda t: (t.opened, t.id))
        allowed = int(len(valid) * HIGH_PRIORITY_LIMIT // 100)
        for ticket in high[allowed:]:
            ticket.downgraded, ticket.priority = True, 'P3'
        excess[invoice] = {'allowed': allowed, 'excess': sorted(t.id for t in high[allowed:])}
    return excess


def _in_scope(ticket, scope, entity_invoice):
    return scope == 'geral' or scope_of(ticket, entity_invoice) == scope


def evaluate(snapshot: GlpiSnapshot, period: Competence, holidays, entity_invoice):
    start = datetime.combine(period.start, time.min)
    end = datetime.combine(period.end + timedelta(days=1), time.min)
    cut = min(snapshot.cut or end, end)
    clock = ContractClock(holidays, CONTRACT_START - timedelta(days=1), period.end + timedelta(days=40))
    logs = {}
    for e in snapshot.logs:
        logs.setdefault(to_int(e['items_id']), []).append(e)
    slas = {to_int(s['id']): s for s in snapshot.slas}
    satisfaction = {to_int(s['tickets_id']): s for s in snapshot.satisfactions}
    tickets, diagnostics = [], {'excluded_before_contract': 0, 'deleted': 0}
    for row in snapshot.tickets:
        if to_int(row.get('is_deleted')):
            diagnostics['deleted'] += 1
            continue
        ticket = Ticket(row, logs.get(to_int(row['id']), []), snapshot.zabbix_requesters)
        ticket.no_resolution_category = str(snapshot.categories.get(ticket.category) or '').strip().upper() in NO_RESOLUTION_CATEGORIES
        if ticket.opened is None or ticket.opened < CONTRACT_OPENING:
            diagnostics['excluded_before_contract'] += 1
            continue
        if ticket.opened >= cut:
            continue
        tickets.append(ticket)
    solved = [t for t in tickets if t.solved and start <= t.solved < cut]
    excess = apply_high_priority_limit(solved, entity_invoice)
    window_end = add_business_days(end - timedelta(seconds=1), REOPEN_DAYS, holidays)
    observed = snapshot.observed or cut
    result = {'period': period, 'cut': cut, 'partial': cut < end, 'scopes': {}, 'trs45': [], 'reopen_events': [],
              'reopen_window_end': window_end, 'reopen_provisional': observed < window_end}
    for scope in SCOPES:
        cohort = [t for t in solved if _in_scope(t, scope, entity_invoice)]
        result['scopes'][scope] = _scope(cohort, tickets, scope, entity_invoice, period, start, cut, holidays, slas, satisfaction,
                                         snapshot, result, clock, excess)
    for ticket in solved:
        limit = ticket.resolve_limit(slas)
        if not limit:
            continue
        net = ticket.net_seconds(clock, ticket.solved)
        if net > 3 * limit:
            result['trs45'].append({'ticket': ticket.id, 'scope': scope_of(ticket, entity_invoice), 'priority': ticket.priority,
                                    'original_priority': ticket.original_priority, 'p3_by_limit': ticket.downgraded,
                                    'type': ticket.type, 'zabbix': ticket.zabbix, 'opened': ticket.opened, 'solved': ticket.solved,
                                    'sla': slas[ticket.sla_ttr].get('name'), 'limit_seconds': limit, 'net_seconds': net,
                                    'state': 'solucionado'})
    sla_by_id = slas
    result['diagnostics'] = {
        **diagnostics,
        'universe': len(tickets),
        'solved_in_period': len(solved),
        'invalid_priority': sorted(t.id for t in solved if t.priority is None),
        'unassigned_entity': sorted(t.id for t in solved if scope_of(t, entity_invoice) == 'nao_atribuido'),
        'transferred': sorted(t.id for t in solved if t.entity_logs),
        'category_without_resolution_sla': sorted(t.id for t in solved if t.no_resolution_category and t.sla_ttr),
        'zabbix_with_business_sla': sorted(t.id for t in solved if t.zabbix and t.sla_ttr and
                                           to_int(sla_by_id.get(t.sla_ttr, {}).get('calendars_id')) not in CALENDARS_24X7),
        'unsupported_sla': sorted(t.id for t in tickets if t.sla_ttr and sla_seconds(slas.get(t.sla_ttr)) is None),
    }
    return result


def _ratio(numerator, denominator, goal, direction='minimum'):
    return measure(numerator, denominator, goal=goal, direction=direction)


def _scope(cohort, tickets, scope, entity_invoice, period, start, cut, holidays, slas, satisfaction, snapshot, result, clock, excess):
    valid = [t for t in cohort if t.priority]
    goal95 = adjusted_goal('95', 'minimum', period)
    out = {'tickets': len(cohort), 'invalid_priority': len(cohort) - len(valid)}
    ins02 = {}
    for p in PRIORITIES:
        group = [t for t in valid if t.priority == p]
        with_sla = [t for t in group if t.sla_tto and t.time_to_own]
        on_time = [t for t in with_sla if t.owned_on_time(clock)]
        ins02[p] = {**_ratio(len(on_time), len(with_sla), goal95), 'total': len(group), 'without_sla': len(group) - len(with_sla),
                    'without_event': sum(1 for t in with_sla if t.taken is None),
                    'late': sorted(t.id for t in with_sla if not t.owned_on_time(clock))}
    out['INS02'] = ins02
    for code, kind in (('INS03', INCIDENT), ('INS04', REQUEST)):
        by_priority = {}
        for p in PRIORITIES:
            group = [t for t in valid if t.priority == p and t.type == kind]
            excluded = sum(1 for t in group if t.no_resolution_category)
            with_sla = [t for t in group if t.sla_ttr and t.time_to_resolve and not t.no_resolution_category]
            on_time = [t for t in with_sla if t.solved_on_time(clock)]
            by_priority[p] = {**_ratio(len(on_time), len(with_sla), goal95), 'total': len(group),
                              'without_sla': len(group) - len(with_sla) - excluded, 'no_resolution_category': excluded,
                              'without_event': 0,
                              'late': sorted(t.id for t in with_sla if not t.solved_on_time(clock))}
        out[code] = by_priority
    end = datetime.combine(period.end + timedelta(days=1), time.min)
    concluded = [t for t in tickets if _in_scope(t, scope, entity_invoice) and any(start <= c < min(cut, end) for c in t.conclusions)]
    events = []
    for t in concluded:
        for moment in t.reopenings():
            previous = [c for c in t.conclusions if c <= moment]
            if not previous:
                continue
            conclusion = previous[-1]
            if start <= conclusion < end and moment <= add_business_days(conclusion, REOPEN_DAYS, holidays):
                events.append({'ticket': t.id, 'conclusion': conclusion, 'reopened': moment, 'priority': t.priority})
    if scope == 'geral':
        result['reopen_events'] = events
    out['INS05'] = {**_ratio(len(events), len(concluded), adjusted_goal('8', 'maximum', period), 'maximum'),
                    'concluded': len(concluded), 'events': len(events)}
    goal_knowledge = knowledge_goal(period)
    for code, links in (('INS06', snapshot.knowledge_links), ('INS07', snapshot.item_links)):
        linked = [t for t in cohort if t.id in links]
        out[code] = {**_ratio(len(linked), len(cohort), goal_knowledge), 'linked': len(linked)}
    rated = [satisfaction[t.id] for t in cohort if t.id in satisfaction and to_datetime(satisfaction[t.id].get('date_answered'))
             and to_int(satisfaction[t.id].get('satisfaction')) in (1, 2, 3, 4, 5)]
    good = [s for s in rated if to_int(s.get('satisfaction')) >= 4]
    out['INS01'] = {**_ratio(len(good), len(rated), adjusted_goal('80', 'minimum', period)), 'rated': len(rated)}
    high = [t for t in valid if t.original_priority in ('P1', 'P2')]
    downgraded = [t for t in valid if t.downgraded]
    out['P1P2'] = {'share': (Decimal(len(high)) / len(valid) * 100) if valid else None, 'high': len(high), 'valid': len(valid),
                   'limit': HIGH_PRIORITY_LIMIT, 'allowed': excess[scope]['allowed'] if scope in excess else None,
                   'excess': len(downgraded),
                   'rule': 'TR 8.10 e 8.11 por item: acima de 60% de P1+P2, os últimos abertos têm prazo de P3 (resposta de 18/10/2026)'}
    return out


REQUIRED_TABLES = ('glpi_tickets', 'glpi_logs', 'glpi_slas', 'glpi_tickets_users')
OPTIONAL_TABLES = ('glpi_ticketsatisfactions', 'glpi_items_tickets', 'glpi_knowbaseitems_items', 'glpi_itilcategories')
TICKET_COLUMNS = ('id', 'entities_id', 'date', 'solvedate', 'takeintoaccountdate', 'time_to_own', 'time_to_resolve', 'status',
                  'priority', 'type', 'requesttypes_id', 'is_deleted', 'slas_id_tto', 'slas_id_ttr', 'begin_waiting_date', 'itilcategories_id')


def load_snapshot(connection, database, period: Competence, cut: datetime, observed: datetime | None = None):
    """Lê a cópia local do GLPI (colunas texto) só com o necessário para a competência."""
    from sqlalchemy import text

    def q(identifier):
        if not identifier.replace('_', '').isalnum():
            raise ValueError('Identificador inválido.')
        return f'`{identifier}`'

    tables = set(connection.execute(text('SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = :db'), {'db': database}).scalars())
    missing = [t for t in REQUIRED_TABLES if t not in tables]
    if missing:
        raise LookupError('A cópia local do GLPI não tem as tabelas: ' + ', '.join(missing) + '. Rode o conector GLPI.')
    db = q(database)
    # A reabertura de uma conclusão do fim da competência acontece depois do corte: o histórico de
    # status vai até 14 dias além do fim, limitado ao momento da leitura, e o motor filtra pela janela.
    observed_until = min(observed or cut, datetime.combine(period.end + timedelta(days=1), time.min) + REOPEN_LOOKAHEAD)
    history = max(cut, observed_until)
    start = datetime.combine(period.start, time.min).strftime('%Y-%m-%d %H:%M:%S')
    columns = ', '.join(f't.{q(c)}' for c in TICKET_COLUMNS)
    tickets = [dict(r) for r in connection.execute(text(
        f"SELECT {columns} FROM {db}.`glpi_tickets` t WHERE t.`date` >= :opening AND t.`date` < :cut "
        f"AND (t.`solvedate` IS NULL OR t.`solvedate` = '' OR t.`solvedate` >= :start)"),
        {'opening': CONTRACT_OPENING.strftime('%Y-%m-%d %H:%M:%S'), 'cut': cut.strftime('%Y-%m-%d %H:%M:%S'), 'start': start}).mappings()]
    ids = [str(to_int(t['id'])) for t in tickets]
    logs, zabbix, satisfactions, knowledge, items = [], set(), [], set(), set()
    for i in range(0, len(ids), 500):
        chunk = {f'i{n}': v for n, v in enumerate(ids[i:i + 500])}
        marks = ', '.join(':' + k for k in chunk)
        logs += [dict(r) for r in connection.execute(text(
            f"SELECT `id`, `items_id`, `date_mod`, `id_search_option`, `old_value`, `new_value` FROM {db}.`glpi_logs` "
            f"WHERE `itemtype` = 'Ticket' AND `items_id` IN ({marks}) AND `id_search_option` IN ('12', '17', '80') AND `date_mod` <= :history"),
            {**chunk, 'history': history.strftime('%Y-%m-%d %H:%M:%S')}).mappings()]
        zabbix |= {to_int(v) for v in connection.execute(text(
            f"SELECT `tickets_id` FROM {db}.`glpi_tickets_users` WHERE `tickets_id` IN ({marks}) AND `type` = '1' AND `users_id` = :u"),
            {**chunk, 'u': str(ZABBIX_REQUESTER)}).scalars()}
        if 'glpi_ticketsatisfactions' in tables:
            satisfactions += [dict(r) for r in connection.execute(text(
                f"SELECT `tickets_id`, `date_answered`, `satisfaction` FROM {db}.`glpi_ticketsatisfactions` WHERE `tickets_id` IN ({marks})"),
                chunk).mappings()]
        if 'glpi_items_tickets' in tables:
            items |= {to_int(v) for v in connection.execute(text(
                f"SELECT `tickets_id` FROM {db}.`glpi_items_tickets` WHERE `tickets_id` IN ({marks})"), chunk).scalars()}
        if 'glpi_knowbaseitems_items' in tables:
            knowledge |= {to_int(v) for v in connection.execute(text(
                f"SELECT `items_id` FROM {db}.`glpi_knowbaseitems_items` WHERE `itemtype` = 'Ticket' AND `items_id` IN ({marks})"),
                chunk).scalars()}
    categories = {}
    if 'glpi_itilcategories' in tables:
        categories = {to_int(i): c for i, c in connection.execute(text(f"SELECT `id`, `code` FROM {db}.`glpi_itilcategories`"))}
    slas = [dict(r) for r in connection.execute(text(
        f"SELECT `id`, `name`, `number_time`, `definition_time`, `end_of_working_day`, `calendars_id` FROM {db}.`glpi_slas`")).mappings()]
    snapshot = GlpiSnapshot(tickets=tickets, logs=logs, slas=slas, satisfactions=satisfactions, knowledge_links=knowledge,
                            item_links=items, zabbix_requesters=zabbix, categories=categories, cut=cut, observed=observed_until)
    return snapshot, [t for t in OPTIONAL_TABLES if t not in tables]
