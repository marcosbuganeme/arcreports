"""Limites temporais compartilhados por consultas, detalhes e exportações."""
from __future__ import annotations
from datetime import date, datetime, time, timedelta


def period_params(start: date | str, end: date | str) -> dict:
    start = date.fromisoformat(start) if isinstance(start, str) else start
    end = date.fromisoformat(end) if isinstance(end, str) else end
    if start > end or end == date.max:
        raise ValueError("Período inválido: confira as datas inicial e final.")
    return {
        "date_from": datetime.combine(start, time.min),
        # Compatibilidade com SQLs cadastrados que já usam date_to inclusivo.
        "date_to": datetime.combine(end, time.max),
        "date_to_exclusive": datetime.combine(end + timedelta(days=1), time.min),
    }


def period_expression(column: str, column_type: str | None = None) -> str:
    if column_type and any(t in column_type.upper() for t in ("TEXT", "CHAR")):
        formats = ("%Y-%m-%d %H:%i:%s.%f", "%Y-%m-%d %H:%i:%s", "%Y-%m-%d %H:%i", "%Y-%m-%d",
                   "%d/%m/%Y %H:%i:%s.%f", "%d/%m/%Y %H:%i:%s", "%d/%m/%Y %H:%i", "%d/%m/%Y")
        column = "COALESCE(" + ", ".join(f"STR_TO_DATE({column}, '{fmt}')" for fmt in formats) + ")"
    return f"({column} >= :date_from AND {column} < :date_to_exclusive)"
