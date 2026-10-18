import csv
import hashlib
from io import StringIO
import os
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.models import UraDaily, UraImportRun
from app.ura import HEADERS, import_ura_csv, parse_ura_csv


def sample_csv(*, incoming=12, answered=10, answered_30=8, day='22/09/2026'):
    header = ['unused'] * 31
    for index,name in HEADERS.items():
        header[index] = name
    row = ['0'] * 31
    row[0:3] = ['531','DET_GO - TESTE',day]
    row[3:7] = [str(incoming),str(answered),'2','0']
    row[19:23] = [str(answered_30),'1',str(answered-answered_30),'1']
    row[25:29] = row[19:23]
    result = StringIO(newline='')
    csv.writer(result,delimiter=';').writerows([header,row])
    return result.getvalue().encode()


def test_idempotence_provenance_and_project_isolation(db_session):
    raw = sample_csv()
    first = import_ura_csv(db_session,raw, project='detran-go', source_name='folder/test.csv',username='auditor')
    assert first.sha256 == hashlib.sha256(raw).hexdigest()
    assert first.raw_csv.encode() == raw
    second = import_ura_csv(db_session,raw, project='DETRAN-GO', source_name='test.csv',username='auditor')
    assert (second.inserted_count, second.unchanged_count) == (0,1)
    assert db_session.scalar(select(func.count()).select_from(UraDaily)) == 1
    assert db_session.scalar(select(UraDaily)).import_id == first.id
    import_ura_csv(db_session,raw,project='OTHER',source_name='same.csv',username='auditor')
    assert db_session.scalar(select(func.count()).select_from(UraDaily)) == 2


def test_conflict_is_atomic(db_session):
    import_ura_csv(db_session,sample_csv(), project='DETRAN-GO',source_name='one.csv',username='auditor')
    # Arquivo com registro novo antes do conflito: nada desse lote pode persistir.
    first_lines = sample_csv(day='23/09/2026').decode().splitlines()
    conflict = sample_csv(answered=9).decode().splitlines()[1]
    raw = ('\n'.join(first_lines+[conflict])+'\n').encode()
    with pytest.raises(ValueError, match='Conflito'):
        import_ura_csv(db_session,raw,project='DETRAN-GO',source_name='changed.csv',username='auditor')
    assert db_session.scalar(select(func.count()).select_from(UraDaily)) == 1
    assert db_session.scalar(select(func.count()).select_from(UraImportRun)) == 1


def test_repeated_blocks_must_agree_and_counts_not_summed_twice():
    raw = sample_csv()
    _,records = parse_ura_csv(raw)
    assert records[0]['answered_30'] == 8
    bad = list(csv.reader(StringIO(raw.decode()),delimiter=';'))
    bad[1][25] = '7'
    output = StringIO()
    csv.writer(output,delimiter=';').writerows(bad)
    with pytest.raises(ValueError,match='divergentes'):
        parse_ura_csv(output.getvalue().encode())


def test_real_reference_csv_twice(db_session):
    source = os.environ.get('URA_REFERENCE_CSV')
    if not source:
        pytest.skip('Defina URA_REFERENCE_CSV para conferir o arquivo real sem versioná-lo.')
    raw = Path(source).read_bytes()
    for _ in range(2):
        result = import_ura_csv(db_session,raw,project='DETRAN-GO',source_name=Path(source).name,username='isolated-test')
    assert (result.record_count,result.inserted_count,result.unchanged_count) == (104,0,104)
    assert db_session.execute(select(func.count(),func.sum(UraDaily.incoming),func.sum(UraDaily.answered),func.sum(UraDaily.answered_30))).one() == (104,779,767,733)
