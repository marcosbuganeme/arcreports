"""Pontuação da Tabela 25, ponderação PAO e teto de ajuste por fatura e geral."""
from decimal import Decimal, ROUND_FLOOR

from app.billing_rules import pao_factor

TABLE_25 = {
    'INS01': (Decimal(5), Decimal(1)),
    'INS02': (Decimal(5), Decimal(1)),
    'INS03': (Decimal(5), Decimal(1)),
    'INS04': (Decimal(5), Decimal(1)),
    'INS05': (Decimal(5), Decimal(1)),
    'INS06': (Decimal(5), Decimal(1)),
    'INS07': (Decimal(5), Decimal(1)),
    'INS08': (Decimal(10), Decimal(1)),
    'INS09': (Decimal(10), Decimal(1)),
    'INS10': (Decimal(10), Decimal(1)),
    'INS11': (Decimal(25), Decimal(1)),
    'INS12': (Decimal(5), Decimal(1)),
    'INS13': (Decimal(5), Decimal(1)),
    'IND01': (Decimal(10), Decimal('0.1')),
    'IND02': (Decimal(10), Decimal('0.1')),
}
PENDING_SCALE = {}
TRS_POINTS = {'TRS45': Decimal(5)}
POLICIES = ('proporcional', 'truncado')
ADJUSTMENT_CAP = Decimal(30)


def deviation(value, goal, direction):
    if value is None or goal is None:
        return None
    gap = goal - value if direction == 'minimum' else value - goal
    return max(Decimal(0), gap)


def indicator_points(code, value, goal, direction, period):
    """Pontos-base por política de fração (P4) e ponderados pelo PAO da competência."""
    factor = pao_factor(period)
    result = {'deviation': deviation(value, goal, direction), 'factor': factor, 'pending': PENDING_SCALE.get(code)}
    for policy in POLICIES:
        result[policy] = None
        result[policy + '_base'] = None
    if result['deviation'] is None or result['pending']:
        return result
    points, step = TABLE_25[code]
    steps = result['deviation'] / step
    for policy in POLICIES:
        counted = steps if policy == 'proporcional' else steps.to_integral_value(rounding=ROUND_FLOOR)
        base = counted * points
        result[policy + '_base'] = base
        result[policy] = base * factor
    return result


def occurrence_points(code, occurrences, period):
    base = TRS_POINTS[code] * occurrences
    return {'occurrences': occurrences, 'base': base, 'factor': pao_factor(period), 'weighted': base * pao_factor(period)}


def adjustment(points):
    """Ajuste = pontos ÷ 10, em %; o teto de 30% guarda calculado, aplicado e excedente."""
    if points is None:
        return None
    calculated = points / 10
    applied = min(calculated, ADJUSTMENT_CAP)
    return {'points': points, 'calculated': calculated, 'applied': applied, 'excess': calculated - applied}
