"""Integração opcional: somente banco descartável local explicitamente habilitado."""
from datetime import date
from decimal import Decimal
import os
from pathlib import Path
from uuid import uuid4

from alembic import context
from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import create_engine, inspect, select, func, text
from sqlalchemy.engine import make_url
from sqlalchemy.schema import CreateTable
from sqlalchemy.orm import Session

from app.database import Base
from app.models import DashboardSource, DashboardWidget, UraDaily
from app.periods import period_expression, period_params
from app.routes import dashboard
from app.ura import import_ura_csv
from tests.test_contract_migrations import load_revision


def test_mariadb_period_migration_csv_and_numeric_gauges(monkeypatch):
    url = os.environ.get('ARCREPORTS_TEST_MARIADB_URL')
    if not url:
        pytest.skip('Defina ARCREPORTS_TEST_MARIADB_URL para o MariaDB descartável local.')
    parsed = make_url(url)
    assert parsed.host == '127.0.0.1' and parsed.port == 13367 and parsed.database == 'arc_rd' and parsed.username == 'arc_rd'
    engine = create_engine(url)
    # Primeiro ensaio cria esquema predecessor conservando as colunas existentes.
    if 'dashboard_widgets' not in inspect(engine).get_table_names():
        Base.metadata.create_all(engine,tables=[t for t in Base.metadata.sorted_tables if t.name not in {'dashboard_widgets','ura_daily','ura_import_runs'}])
        ddl = str(CreateTable(DashboardWidget.__table__).compile(engine))
        ddl = '\n'.join(line for line in ddl.splitlines() if not any(field in line for field in ('gauge_mode','goal_direction','denominator_field')))
        with engine.begin() as connection:
            connection.execute(text(ddl))
    monkeypatch.setattr(context,'is_offline_mode',lambda:False)
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            for _ in range(2):
                load_revision('20261017_rd09_gauge_operations.py').upgrade()
                load_revision('20261017_rd10_ura_daily.py').upgrade()
    columns = {c['name'] for c in inspect(engine).get_columns('dashboard_widgets')}
    assert {'gauge_mode','goal_direction','denominator_field'} <= columns
    table = 'custom_rd_'+uuid4().hex[:12]
    with engine.begin() as connection:
        connection.execute(text(f'CREATE TABLE `{table}` (native_date DATE, native_datetime DATETIME(6), iso_text LONGTEXT, br_text VARCHAR(80), numerator INT, denominator INT, fraction DECIMAL(30,20))'))
        for index, stamp in enumerate(['2026-10-21 00:00:00','2026-10-21 12:00:00','2026-10-21 23:59:59.999999','2026-10-22 00:00:00']):
            clock = stamp[11:]
            connection.execute(text(f'INSERT INTO `{table}` VALUES (:d,:dt,:iso,:br,:n,:den,:fraction)'),
                {'d':stamp[:10],'dt':stamp,'iso':stamp,'br':('21/10/2026' if index < 3 else '22/10/2026')+' '+clock,
                 'n':255 if index == 0 else 256,'den':259 if index == 0 else 260,'fraction':1 if index in (0,3) else 0})
        for column,kind in [('native_date','DATE'),('native_datetime','DATETIME(6)'),('iso_text','LONGTEXT'),('br_text','VARCHAR(80)')]:
            count = connection.execute(text(f'SELECT COUNT(*) FROM `{table}` WHERE '+period_expression('`'+column+'`',kind)),period_params(date(2026,9,22),date(2026,10,21))).scalar()
            assert count == 3, (column,count)
    monkeypatch.setattr(dashboard,'local_engine',engine)
    monkeypatch.setattr(dashboard.settings,'local_db_name','arc_rd')
    source = DashboardSource(name='Local test',source_table=table,default_time_field='native_datetime')
    widget = DashboardWidget(source=source,widget_type='gauge',gauge_mode='ratio',goal_direction='minimum',meta_gauge=95,
                             aggregation='sum',value_field='numerator',denominator_field='denominator')
    result = {}
    dashboard.apply_widget_gauge(result,widget,'native_datetime',date(2026,9,22),date(2026,10,21))
    assert result['metric']['value'] == Decimal(767)/779*100
    widget.gauge_mode, widget.aggregation, widget.value_field = 'value','avg','fraction'
    dashboard.apply_widget_gauge(result,widget,'native_datetime',date(2026,9,22),date(2026,10,21))
    assert str(result['metric']['value']).startswith('0.33333333333333333333')
    widget.gauge_mode, widget.value_field = 'ratio','numerator'
    dashboard.apply_widget_gauge(result,widget,'native_datetime',date(2026,8,22),date(2026,9,21))
    assert result['metric']['state'] == 'no_sample'
    reference = os.environ.get('URA_REFERENCE_CSV')
    assert reference, 'Também defina URA_REFERENCE_CSV para a prova da importação original.'
    with Session(engine) as session:
        for _ in range(2):
            run = import_ura_csv(session,Path(reference).read_bytes(),project='DETRAN-GO',source_name=Path(reference).name,username='isolated-mariadb')
        assert run.inserted_count == 0 and run.unchanged_count == 104
        assert session.execute(select(func.count(),func.sum(UraDaily.incoming),func.sum(UraDaily.answered),func.sum(UraDaily.answered_30)).where(UraDaily.project == 'DETRAN-GO')).one() == (104,779,767,733)
    engine.dispose()
