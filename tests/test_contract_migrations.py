import importlib.util
from pathlib import Path

from alembic import context
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text


def load_revision(name):
    path = Path('alembic/versions') / name
    spec = importlib.util.spec_from_file_location(path.stem,path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_existing_install_and_startup_compatibility(monkeypatch):
    monkeypatch.setattr(context,'is_offline_mode',lambda:False)
    engine = create_engine('sqlite://')
    with engine.begin() as conn:
        conn.execute(text('CREATE TABLE dashboard_widgets (id INTEGER PRIMARY KEY, title VARCHAR(160))'))
        conn.execute(text("INSERT INTO dashboard_widgets VALUES (1,'existing')"))
        with Operations.context(MigrationContext.configure(conn)):
            for _ in range(2):
                load_revision('20261017_rd09_gauge_operations.py').upgrade()
                load_revision('20261017_rd10_ura_daily.py').upgrade()
        row = conn.execute(text('SELECT title,gauge_mode,goal_direction,denominator_field FROM dashboard_widgets')).one()
        assert row == ('existing','legacy','minimum',None)
        assert {'ura_daily','ura_import_runs'} <= set(inspect(conn).get_table_names())
        assert any(c['column_names'] == ['project','service_id','day'] for c in inspect(conn).get_unique_constraints('ura_daily'))


def test_rd13_creates_contract_tables_and_ura_columns_idempotently(monkeypatch):
    from app.models import CONTRACT_INTERNAL_TABLES
    monkeypatch.setattr(context,'is_offline_mode',lambda:False)
    engine = create_engine('sqlite://')
    with engine.begin() as conn:
        conn.execute(text('CREATE TABLE dashboard_widgets (id INTEGER PRIMARY KEY, title VARCHAR(160))'))
        with Operations.context(MigrationContext.configure(conn)):
            load_revision('20261017_rd09_gauge_operations.py').upgrade()
            load_revision('20261017_rd10_ura_daily.py').upgrade()
            conn.execute(text("INSERT INTO ura_import_runs (id,project,source_name,sha256,raw_csv,imported_at,username,record_count,inserted_count,unchanged_count) VALUES (1,'P','a.csv','x','',CURRENT_TIMESTAMP,'u',1,1,0)"))
            for _ in range(2):
                load_revision('20261018_rd13_contract_registries.py').upgrade()
        assert CONTRACT_INTERNAL_TABLES <= set(inspect(conn).get_table_names())
        assert conn.execute(text('SELECT replaced_count, reason FROM ura_import_runs')).one() == (0, None)
    rd13 = load_revision('20261018_rd13_contract_registries.py')
    assert (rd13.revision, rd13.down_revision) == ('20261018_rd13', '20261017_rd10')
