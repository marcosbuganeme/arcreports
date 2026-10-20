from types import SimpleNamespace

from app.routes.dashboard import entity_filter_clause, get_source_count


def _source(table="reports.nms_teste"):
    return SimpleNamespace(id=1, source_table=table, default_time_field=None)


def test_entity_clause_uses_bound_parameter(monkeypatch):
    monkeypatch.setattr("app.routes.dashboard.dashboard_column_names", lambda source: {"entidade", "data"})

    clause, params = entity_filter_clause(_source(), "Central TI' OR 1=1 --")

    assert clause == "`entidade` = :entity_filter"
    assert params == {"entity_filter": "Central TI' OR 1=1 --"}


def test_entity_clause_ignored_when_source_has_no_column(monkeypatch):
    monkeypatch.setattr("app.routes.dashboard.dashboard_column_names", lambda source: {"data"})

    assert entity_filter_clause(_source(), "Central TI") == (None, {})


def test_entity_clause_ignored_without_entity(monkeypatch):
    monkeypatch.setattr("app.routes.dashboard.dashboard_column_names", lambda source: {"entidade"})

    assert entity_filter_clause(_source(), None) == (None, {})
    assert entity_filter_clause(_source(), "") == (None, {})


def test_source_count_combines_period_and_entity(monkeypatch):
    captured = {}
    monkeypatch.setattr("app.routes.dashboard.dashboard_column_names", lambda source: {"entidade", "data"})
    monkeypatch.setattr("app.routes.dashboard.source_time_field", lambda source, columns: "data")
    monkeypatch.setattr("app.routes.dashboard._get_column_type", lambda table, column, database: "datetime")
    monkeypatch.setattr("app.routes.dashboard.apply_query_timeout", lambda connection: None)

    class Result:
        def scalar(self):
            return 7

    class Connection:
        def execute(self, statement, params=None):
            captured["sql"] = str(statement)
            captured["params"] = params
            return Result()

    class ConnectContext:
        def __enter__(self):
            return Connection()

        def __exit__(self, exc_type, exc, traceback):
            return False

    monkeypatch.setattr("app.routes.dashboard.local_engine.connect", lambda: ConnectContext())

    total = get_source_count(None, _source(), "2026-09-01", "2026-09-30", "Central TI")

    assert total == 7
    assert " AND `entidade` = :entity_filter" in captured["sql"]
    assert captured["params"]["entity_filter"] == "Central TI"
    assert captured["params"]["date_from"] == "2026-09-01"
