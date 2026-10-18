"""Medidores com direção explícita e ausência de amostra distinta de zero."""
from app.billing_rules import decimal_value


def measure(numerator, denominator=None, *, mode='ratio', goal=None, direction='minimum'):
    if mode not in {'ratio', 'value'} or direction not in {'minimum', 'maximum'}:
        raise ValueError('Operação ou direção do medidor inválida.')
    num = decimal_value(numerator) if numerator is not None else None
    den = decimal_value(denominator) if denominator is not None else None
    value = None if num is None or (mode == 'ratio' and not den) else num / den * 100 if mode == 'ratio' else num
    target = decimal_value(goal) if goal is not None else None
    meets = None if value is None or target is None else value >= target if direction == 'minimum' else value <= target
    return {'value': value, 'numerator': num, 'denominator': den, 'goal': target,
            'direction': direction, 'symbol': '≥' if direction == 'minimum' else '≤',
            'meets_goal': meets, 'state': 'no_sample' if value is None else 'measured'}
