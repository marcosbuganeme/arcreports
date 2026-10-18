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
