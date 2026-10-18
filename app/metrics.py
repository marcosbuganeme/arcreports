"""Medidores com direção explícita e ausência de amostra distinta de zero."""
from decimal import Decimal, InvalidOperation
from app.billing_rules import decimal_value


def measured_decimal(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("Valor medido inválido.") from exc
    if not result.is_finite():
        raise ValueError("Valor medido não finito.")
    return result


def measure(numerator, denominator=None, *, mode='ratio', goal=None, direction='minimum'):
    if mode not in {'ratio', 'value'} or direction not in {'minimum', 'maximum'}:
        raise ValueError('Operação ou direção do medidor inválida.')
    num = measured_decimal(numerator) if numerator is not None else None
    den = measured_decimal(denominator) if denominator is not None else None
    if mode == 'ratio' and ((num is not None and num < 0) or (den is not None and den < 0)):
        raise ValueError('Contagens de razão não podem ser negativas.')
    value = None if num is None or (mode == 'ratio' and not den) else num / den * 100 if mode == 'ratio' else num
    target = decimal_value(goal) if goal is not None else None
    meets = None if value is None or target is None else value >= target if direction == 'minimum' else value <= target
    return {'value': value, 'numerator': num, 'denominator': den, 'goal': target,
            'direction': direction, 'symbol': '≥' if direction == 'minimum' else '≤',
            'meets_goal': meets, 'state': 'no_sample' if value is None else 'measured'}
