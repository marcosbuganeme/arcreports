from datetime import date, datetime

import pytest
from sqlalchemy import create_engine, text

from app.periods import period_expression, period_params
from app.routes.dashboard import _period_filter_expr, date_period_params


def test_fractional_last_second_and_next_day():
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE events (event_date DATETIME)"))
        for value in ("2026-10-21 00:00:00", "2026-10-21 12:00:00", "2026-10-21 23:59:59.999999", "2026-10-22 00:00:00"):
            conn.execute(text("INSERT INTO events VALUES (:value)"), {"value":value})
        rows = conn.execute(text("SELECT event_date FROM events WHERE " + period_expression("event_date")), period_params(date(2026,9,22), date(2026,10,21))).all()
    assert len(rows) == 3
    assert date_period_params(date(2026,9,22), date(2026,10,21))["date_to_exclusive"] == datetime(2026,10,22)


@pytest.mark.parametrize("kind", ["DATE", "DATETIME(6)", "LONGTEXT", "VARCHAR(255)"])
def test_typed_period_expression(kind):
    sql = _period_filter_expr("`event_date`",kind)
    assert ">= :date_from" in sql and "< :date_to_exclusive" in sql
    assert ("STR_TO_DATE" in sql) == (kind in ("LONGTEXT", "VARCHAR(255)"))


def test_invalid_period():
    with pytest.raises(ValueError):
        period_params("2026-10-22", "2026-10-21")

@pytest.mark.parametrize('kind', ['datetime', 'longtext'])
def test_builder_preserves_end_boundary(monkeypatch, kind):
    from app.routes import reports
    monkeypatch.setattr(reports, 'parse_source', lambda _: ('reports', 'events'))
    monkeypatch.setattr(reports, 'get_builder_columns', lambda *_: [{'name':'event_date', 'data_type':kind}])
    sql, _ = reports.build_visual_report({'builder_source':'reports.events', 'builder_fields':'event_date', 'filter_field_1':'event_date', 'filter_operator_1':'between_dates', 'filter_value_1':'2026-09-22', 'filter_value_end_1':'2026-10-21'})
    assert "< '2026-10-22'" in sql
    assert ("STR_TO_DATE" in sql) == (kind == 'longtext')
