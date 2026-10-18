"""Integração opcional: cópia local do GLPI (colunas texto, como o conector grava) num MariaDB descartável."""
from datetime import datetime
import os
from uuid import uuid4

from alembic import context
from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app import contract_apuracao
from app.billing_rules import Competence
from app.database import Base
from app.glpi_contract import load_snapshot, evaluate
from app.business_calendar import holiday_set
from app.models import CONTRACT_INTERNAL_TABLES
from tests.test_contract_migrations import load_revision

COLUMNS = {
    'glpi_tickets': ('id', 'entities_id', 'date', 'solvedate', 'takeintoaccountdate', 'time_to_own', 'time_to_resolve', 'status',
                     'priority', 'type', 'requesttypes_id', 'is_deleted', 'slas_id_tto', 'slas_id_ttr', 'begin_waiting_date',
                     'itilcategories_id'),
    'glpi_logs': ('id', 'itemtype', 'items_id', 'date_mod', 'id_search_option', 'old_value', 'new_value'),
    'glpi_slas': ('id', 'name', 'number_time', 'definition_time', 'end_of_working_day', 'calendars_id'),
    'glpi_tickets_users': ('id', 'tickets_id', 'users_id', 'type'),
    'glpi_ticketsatisfactions': ('id', 'tickets_id', 'date_answered', 'satisfaction'),
    'glpi_itilcategories': ('id', 'code', 'completename'),
}
ROWS = {
    'glpi_tickets': [
        ('1', '4', '2026-09-21 23:59:59', '2026-10-01 10:00:00', None, None, '2026-10-01 12:00:00', '6', '5', '1', '1', '0', '0', '1', None, '1400'),
        ('2', '4', '2026-09-22 00:00:00', '2026-09-22 10:00:00', '2026-09-22 07:10:00', '2026-09-22 08:00:00', '2026-09-22 11:00:00', '5', '5', '1', '1', '0', '1', '1', None, '1400'),
        ('3', '7', '2026-10-10 00:00:00', '2026-10-10 06:00:00', None, None, None, '5', '3', '1', '19', '0', '0', '2', None, '1400'),
        ('4', '8', '2026-10-10 00:00:00', '2026-10-11 00:00:00', None, None, None, '5', '3', '1', '1', '0', '0', '2', None, '1515'),
        ('5', '4', '2026-10-01 08:00:00', '2026-10-22 00:00:00', None, None, None, '5', '4', '2', '1', '0', '0', '0', None, '1400'),
        ('6', '7', '2026-10-01 08:00:00', '2026-10-02 08:00:00', None, None, None, '5', '4', '2', '1', '0', '0', '0', None, '1400'),
        ('7', '4', '2026-10-01 08:00:00', '2026-10-27 15:00:00', None, None, None, '5', '4', '2', '1', '0', '0', '0', None, '1400'),
    ],
    'glpi_logs': [('1', 'Ticket', '2', '2026-09-22 10:00:00', '17', '', '2026-09-22 10:00:00'),
                  ('2', 'Ticket', '2', '2026-09-22 10:00:00', '12', '2', '5'),
                  ('3', 'Change', '2', '2026-10-20 10:00:00', '12', '5', '2'),
                  ('4', 'Ticket', '7', '2026-10-20 10:00:00', '17', '', '2026-10-20 10:00:00'),
                  ('5', 'Ticket', '7', '2026-10-23 09:00:00', '12', '5', '2')],
    'glpi_slas': [('1', 'P1 4 h', '4', 'hour', '0', '9'), ('2', 'Zabbix 1 h', '1', 'hour', '0', '10')],
    'glpi_tickets_users': [('1', '4', '1062', '1'), ('2', '2', '1062', '2')],
    'glpi_ticketsatisfactions': [('1', '2', '2026-10-22 09:00:00', '4')],
    'glpi_itilcategories': [('1400', 'CAT-DTGO-0001', 'Atendimento Nível 2'), ('1515', 'cat-dtgo-0032', 'Gestão de Chamados > Triagem > Indevido')],
}


@pytest.fixture
def mariadb():
    url = os.environ.get('ARCREPORTS_TEST_GLPI_MARIADB_URL')
    if not url:
        pytest.skip('Defina ARCREPORTS_TEST_GLPI_MARIADB_URL para um MariaDB descartável em 127.0.0.1.')
    parsed = make_url(url)
    assert parsed.host == '127.0.0.1' and parsed.database == 'arc_rd'
    engine = create_engine(url)
    glpi = 'glpi_local_' + uuid4().hex[:10]
    with engine.begin() as connection:
        connection.execute(text(f'CREATE DATABASE `{glpi}`'))
        for table, columns in COLUMNS.items():
            connection.execute(text(f'CREATE TABLE `{glpi}`.`{table}` (' + ', '.join(f'`{c}` LONGTEXT NULL' for c in columns) + ')'))
            marks = ', '.join(f':c{i}' for i in range(len(columns)))
            for row in ROWS[table]:
                connection.execute(text(f'INSERT INTO `{glpi}`.`{table}` VALUES ({marks})'), {f'c{i}': v for i, v in enumerate(row)})
    try:
        yield engine, glpi
    finally:
        with engine.begin() as connection:
            connection.execute(text(f'DROP DATABASE `{glpi}`'))
        engine.dispose()


def test_load_snapshot_from_text_columns_and_full_build(mariadb, monkeypatch):
    engine, glpi = mariadb
    period = Competence.parse('2026-10')
    Base.metadata.create_all(engine)
    monkeypatch.setattr(context, 'is_offline_mode', lambda: False)
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            for _ in range(2):
                load_revision('20261018_rd13_contract_registries.py').upgrade()
    assert CONTRACT_INTERNAL_TABLES <= set(inspect(engine).get_table_names())
    with Session(engine) as db:
        contract_apuracao.seed_contract_defaults(db)
        holidays = holiday_set(db, datetime(2026, 9, 1).date(), datetime(2026, 12, 31).date())
        with engine.connect() as connection:
            snapshot, missing = load_snapshot(connection, glpi, period, datetime(2026, 10, 22), datetime(2026, 10, 30))
            at_cut, _ = load_snapshot(connection, glpi, period, datetime(2026, 10, 22), datetime(2026, 10, 22))
        assert missing == ['glpi_items_tickets', 'glpi_knowbaseitems_items']
        assert sorted(int(t['id']) for t in snapshot.tickets) == [2, 3, 4, 5, 6, 7]
        assert snapshot.zabbix_requesters == {4}
        assert len(snapshot.logs) == 4 and len(at_cut.logs) == 3 and at_cut.observed == datetime(2026, 10, 22)
        result = evaluate(snapshot, period, holidays, {4: 'item01', 7: 'item02', 8: 'item02'})
        general = result['scopes']['geral']
        assert (general['INS03']['P3']['numerator'], general['INS03']['P3']['denominator']) == (1, 1)
        assert general['INS03']['P3']['no_resolution_category'] == 1 and general['P1P2']['excess'] == 1
        assert general['INS02']['P3']['numerator'] == 1 and general['INS01']['numerator'] == 1
        assert [t['ticket'] for t in result['trs45']] == [3]
        assert result['diagnostics']['category_without_resolution_sla'] == [4]
        assert [e['ticket'] for e in result['reopen_events']] == [7] and not result['reopen_provisional']
        monkeypatch.setattr(contract_apuracao, 'local_engine', engine)
        monkeypatch.setattr(contract_apuracao, 'glpi_database', lambda _db: glpi)
        monkeypatch.setattr(contract_apuracao, 'portal_now', lambda: datetime(2026, 10, 30))
        built = contract_apuracao.build(db, period, 'DETRAN-GO', datetime(2026, 10, 22))
        assert built['glpi_error'] is None and len(built['trs45']) == 1
        assert built['totals']['item02']['trs45']['occurrences'] == 1
