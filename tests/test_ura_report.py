from datetime import date
from decimal import Decimal

from app.billing_rules import Competence
from app.contract_report import ura_report
from app.ura import import_ura_csv
from tests.test_ura_import import sample_csv


def test_report_filters_and_ratio_of_sums(db_session):
    for day, incoming, answered, fast in [('22/09/2026',100,90,80),('23/09/2026',10,10,10),('22/10/2026',100,50,1)]:
        import_ura_csv(db_session,sample_csv(incoming=incoming,answered=answered,answered_30=fast,day=day),project='DETRAN-GO',source_name='sample.csv',username='test')
    report = ura_report(db_session,Competence.parse('2026-10'),'DETRAN-GO')
    m12,m13 = report['metrics']
    assert report['count'] == 2 and report['cut'] == date(2026,9,23) and report['partial']
    assert m12['value'] == Decimal(100)/110*100
    assert m13['value'] == Decimal(90)
    assert m12['goal'] == Decimal('47.5')
    assert ura_report(db_session,Competence.parse('2026-10'),'OTHER')['metrics'][0]['value'] is None
    assert ura_report(db_session,Competence.parse('2026-10'),'DETRAN-GO',999)['count'] == 0


def test_zero_sample_is_distinct_from_zero_result(db_session):
    import_ura_csv(db_session,sample_csv(incoming=0,answered=0,answered_30=0),project='DETRAN-GO',source_name='zero.csv',username='test')
    report = ura_report(db_session,Competence.parse('2026-10'),'DETRAN-GO')
    assert report['count'] == 1
    for metric in report['metrics']:
        assert metric['value'] is None and metric['meets_goal'] is None


def test_original_csv_reference_metrics(db_session):
    import os
    from pathlib import Path
    import pytest
    source = os.environ.get('URA_REFERENCE_CSV')
    if not source:
        pytest.skip('Defina URA_REFERENCE_CSV para conferir a série real.')
    import_ura_csv(db_session,Path(source).read_bytes(),project='DETRAN-GO',source_name='source.csv',username='test')
    report = ura_report(db_session,Competence.parse('2026-10'),'DETRAN-GO')
    assert report['count'] == 104 and report['cut'] == date(2026,10,17)
    assert report['metrics'][0]['value'] == Decimal(767)/779*100
    assert report['metrics'][1]['value'] == Decimal(733)/767*100
    assert report['partial'] is True
    assert len(report['coverage']) == 4
