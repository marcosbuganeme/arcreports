"""Regras contratuais puras: sem conexão, configuração de servidor ou efeitos externos."""
from dataclasses import dataclass
from datetime import date, timedelta
import re


@dataclass(frozen=True)
class Competence:
    year: int
    month: int

    def __post_init__(self):
        date(self.year, self.month, 1)
        if (self.year, self.month) == (1, 1):
            raise ValueError("Competência sem mês anterior representável.")

    @classmethod
    def parse(cls, value: str):
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}", value or ""):
            raise ValueError("Competência inválida. Use AAAA-MM.")
        try:
            return cls(int(value[:4]), int(value[5:]))
        except ValueError as exc:
            raise ValueError("Competência inválida. Use AAAA-MM.") from exc

    @property
    def label(self):
        return f"{self.year:04}-{self.month:02}"

    @property
    def start(self):
        previous_month = date(self.year, self.month, 1) - timedelta(days=1)
        return previous_month.replace(day=22)

    @property
    def end(self):
        return date(self.year, self.month, 21)

    @classmethod
    def containing(cls, value: date):
        if value.day <= 21:
            return cls(value.year, value.month)
        next_month = (value.replace(day=1) + timedelta(days=32)).replace(day=1)
        return cls(next_month.year, next_month.month)


CONTRACT_START = date(2026, 9, 22)


def previous_competence(period: Competence) -> Competence | None:
    previous = Competence.containing(period.start - timedelta(days=1))
    return previous if previous.start >= CONTRACT_START else None


def contract_month(period: Competence) -> int:
    first = Competence.containing(CONTRACT_START)
    month = (period.year - first.year) * 12 + period.month - first.month + 1
    if month < 1:
        raise ValueError('Competência anterior ao início do contrato em 22/09/2026.')
    return month


def pao_factor(period: Competence):
    from decimal import Decimal
    month = contract_month(period)
    return Decimal('0.5') if month <= 2 else Decimal('0.7') if month <= 4 else Decimal('0.8') if month <= 6 else Decimal('1')


def decimal_value(value):
    from decimal import Decimal, InvalidOperation
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError('Informe um número válido, com ponto como separador decimal.') from exc
    if not result.is_finite() or result < 0 or result > Decimal('1e12') or result.as_tuple().exponent < -12:
        raise ValueError('Número fora do intervalo permitido (0 a 10¹²; até 12 casas decimais).')
    return result


def adjusted_goal(base, direction: str, period: Competence):
    base = decimal_value(base)
    if direction not in {'minimum', 'maximum'}:
        raise ValueError('Direção da meta inválida.')
    factor = pao_factor(period)
    return base * factor if direction == 'minimum' else base / factor


def weighted_points(base_points, period: Competence):
    return decimal_value(base_points) * pao_factor(period)


def knowledge_goal(period: Competence):
    """INS06/07: trimestre contínuo, sem reaplicar PAO à meta."""
    from decimal import Decimal
    trimester = (contract_month(period) - 1) // 3
    return Decimal((60, 70, 80, 90, 95)[min(trimester, 4)])


def staffing_window(period: Competence):
    """Somente vigência/janela INS09/10; não presume denominador pendente."""
    if contract_month(period) < 3:
        return None
    previous = Competence.containing(period.start - timedelta(days=1))
    first = Competence.containing(previous.start - timedelta(days=1))
    return first.start, period.end
