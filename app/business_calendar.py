"""Dias úteis do contrato: sem fim de semana, feriados nacionais, de GO, de Goiânia e pontos facultativos."""
from datetime import date, timedelta

from sqlalchemy import select

from app.models import ContractHoliday

GLPI_CALENDAR_SOURCE = 'GLPI, calendário 9 "Horário Comercial 7h às 19h", lido em 17/10/2026'
NATIONAL_SOURCE = 'Feriado nacional (Lei 662/1949, Lei 6.802/1980, Lei 14.759/2023)'
GOIANIA_SOURCE = 'Feriado municipal de Goiânia (conferir no calendário da Prefeitura)'
PROVISIONAL_2027 = 'Previsto como em 2026; confirmar no decreto de 2027'


def easter(year):
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    return date(year, month, (h + l - 7 * m + 114) % 31 + 1)


def national_holidays(year):
    fixed = {(1, 1): 'Confraternização Universal', (4, 21): 'Tiradentes', (5, 1): 'Dia do Trabalho',
             (9, 7): 'Independência do Brasil', (10, 12): 'Nossa Senhora Aparecida', (11, 2): 'Finados',
             (11, 15): 'Proclamação da República', (11, 20): 'Dia Nacional de Zumbi e da Consciência Negra',
             (12, 25): 'Natal'}
    days = {date(year, month, day): name for (month, day), name in fixed.items()}
    days[easter(year) - timedelta(days=2)] = 'Sexta-feira Santa'
    return days


GLPI_CALENDAR_9_2026 = {
    date(2026, 2, 16): ('Carnaval (ponto facultativo)', 'ponto_facultativo'),
    date(2026, 2, 17): ('Carnaval', 'ponto_facultativo'),
    date(2026, 2, 18): ('Quarta-feira de Cinzas', 'ponto_facultativo'),
    date(2026, 4, 2): ('Ponto facultativo da Semana Santa', 'ponto_facultativo'),
    date(2026, 4, 20): ('Ponto facultativo de Tiradentes', 'ponto_facultativo'),
    date(2026, 6, 4): ('Corpus Christi', 'ponto_facultativo'),
    date(2026, 6, 5): ('Ponto facultativo de Corpus Christi', 'ponto_facultativo'),
}
GLPI_CALENDAR_9_PERPETUAL = {
    (10, 30): ('Dia do Servidor Público', 'feriado'),
    (12, 30): ('Réveillon', 'ponto_facultativo'),
    (12, 31): ('Réveillon', 'ponto_facultativo'),
}


GOIANIA_PERPETUAL = {(5, 24): 'Nossa Senhora Auxiliadora, padroeira de Goiânia', (10, 24): 'Aniversário de Goiânia'}
PROVISIONAL_2027_DAYS = {
    date(2027, 2, 8): ('Carnaval (ponto facultativo)', 'ponto_facultativo'),
    date(2027, 2, 9): ('Carnaval', 'ponto_facultativo'),
    date(2027, 2, 10): ('Quarta-feira de Cinzas', 'ponto_facultativo'),
    date(2027, 5, 27): ('Corpus Christi', 'ponto_facultativo'),
    date(2027, 7, 26): ('Fundação da Cidade de Goiás', 'estadual'),
}


def default_holidays():
    rows = {}
    for year in (2026, 2027):
        for day, name in national_holidays(year).items():
            rows[day] = (name, 'nacional', NATIONAL_SOURCE)
    glpi = dict(GLPI_CALENDAR_9_2026)
    for year in (2026, 2027):
        for (month, day), entry in GLPI_CALENDAR_9_PERPETUAL.items():
            glpi[date(year, month, day)] = entry
    for day, (name, kind) in glpi.items():
        rows.setdefault(day, (name, kind, GLPI_CALENDAR_SOURCE))
    for year in (2026, 2027):
        for (month, day), name in GOIANIA_PERPETUAL.items():
            rows.setdefault(date(year, month, day), (name, 'municipal', GOIANIA_SOURCE))
    for day, (name, kind) in PROVISIONAL_2027_DAYS.items():
        rows.setdefault(day, (name, kind, PROVISIONAL_2027))
    return rows


def seed_holidays(db):
    existing = set(db.scalars(select(ContractHoliday.day)))
    for day, (name, kind, source) in sorted(default_holidays().items()):
        if day not in existing:
            db.add(ContractHoliday(day=day, name=name, kind=kind, source=source, updated_by='sistema'))


def holiday_set(db, start, end):
    return set(db.scalars(select(ContractHoliday.day).where(ContractHoliday.day >= start, ContractHoliday.day <= end)))


def business_days(start, end, holidays):
    days, current = [], start
    while current <= end:
        if current.weekday() < 5 and current not in holidays:
            days.append(current)
        current += timedelta(days=1)
    return days


def add_business_days(moment, count, holidays):
    """Mesmo horário no N-ésimo dia útil seguinte (janela de reabertura do INS05)."""
    current, left = moment.date(), count
    while left:
        current += timedelta(days=1)
        if current.weekday() < 5 and current not in holidays:
            left -= 1
    return moment.replace(year=current.year, month=current.month, day=current.day)
