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
