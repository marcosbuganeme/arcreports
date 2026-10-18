"""Importação local e auditável do CSV diário Comunix/4Biz, sem chamadas à API."""
import csv
from datetime import datetime
import hashlib
import json
from io import StringIO
import re

from sqlalchemy import select

from app.models import AdminActionLog, UraDaily, UraDailyRevision, UraImportRun

MAX_CSV_BYTES = 2_000_000
COUNTS = {'incoming':3, 'answered':4, 'abandoned':5, 'blocked':6,
          'answered_30':19, 'abandoned_30':20, 'answered_after_30':21, 'abandoned_after_30':22}
HEADERS = {0:'Cod. Serviço',1:'Serviço',2:'Data',3:'Entrantes',4:'Atendidas',5:'Abandonadas',6:'Desistente / Bloqueada',
           19:'Atendida até 30s',20:'Abandono até 30s',21:'Atendida após 30s',22:'Abandono após 30s',
           25:'Atendida até 30s',26:'Abandono até 30s',27:'Atendida após 30s',28:'Abandono após 30s'}


def normalize_project(project):
    project = (project or '').strip().upper()
    if not re.fullmatch(r'[A-Z0-9][A-Z0-9_-]{0,79}', project):
        raise ValueError('Informe o projeto com letras, números, hífen ou sublinhado (até 80 caracteres).')
    return project


def csv_integer(value, line):
    if not re.fullmatch(r'[0-9]{1,10}', value.strip()) or int(value) > 2147483647:
        raise ValueError(f'Linha {line}: contagem inteira inválida.')
    return int(value)


def parse_ura_csv(raw: bytes):
    if not raw or len(raw) > MAX_CSV_BYTES:
        raise ValueError('O CSV deve ter conteúdo e no máximo 2 MB.')
    try:
        original = raw.decode('utf-8')
    except UnicodeDecodeError as exc:
        raise ValueError('Salve o CSV em UTF-8 antes de importar.') from exc
    reader = csv.reader(StringIO(original.lstrip('\ufeff'), newline=''), delimiter=';', strict=True)
    try:
        header = next(reader, [])
        if len(header) != 31 or any(header[index].strip() != expected for index,expected in HEADERS.items()):
            raise ValueError('Cabeçalho incompatível com o CSV diário Comunix/4Biz de 31 colunas.')
        records, keys = [], set()
        for line, row in enumerate(reader, 2):
            if not row or not any(cell.strip() for cell in row):
                continue
            if len(row) != 31:
                raise ValueError(f'Linha {line}: esperado o conjunto de 31 colunas.')
            try:
                day = datetime.strptime(row[2].strip(), '%d/%m/%Y').date()
            except ValueError as exc:
                raise ValueError(f'Linha {line}: data inválida; use DD/MM/AAAA.') from exc
            service_id = csv_integer(row[0], line)
            name = row[1].strip()
            if not name or len(name) > 255:
                raise ValueError(f'Linha {line}: nome do serviço inválido.')
            counts = {name:csv_integer(row[index],line) for name,index in COUNTS.items()}
            if any(csv_integer(row[i], line) != csv_integer(row[i+6], line) for i in range(19,23)):
                raise ValueError(f'Linha {line}: blocos repetidos de contagens divergentes.')
            if counts['answered_30'] + counts['answered_after_30'] != counts['answered'] or counts['answered'] > counts['incoming']:
                raise ValueError(f'Linha {line}: contagens de atendimento inconsistentes.')
            key = (service_id, day)
            if key in keys:
                raise ValueError(f'Linha {line}: serviço/data repetidos no arquivo.')
            keys.add(key)
            records.append({'service_id':service_id, 'service_name':name, 'day':day, **counts})
    except csv.Error as exc:
        raise ValueError('CSV malformado.') from exc
    if not records:
        raise ValueError('O arquivo não contém registros diários.')
    return original, records


def import_ura_csv(db, raw: bytes, *, project: str, source_name: str, username: str, replace: bool = False, reason: str | None = None):
    project = normalize_project(project)
    original, records = parse_ura_csv(raw)
    source_name = (source_name or 'ura.csv').replace('\\','/').rsplit('/',1)[-1][:255]
    reason = (reason or '').strip()
    if replace and len(reason) < 10:
        raise ValueError('Para substituir dias já importados, informe a justificativa (mínimo de 10 caracteres).')
    try:
        existing = {(row.service_id,row.day):row for row in db.scalars(select(UraDaily).where(
            UraDaily.project == project, UraDaily.day >= min(r['day'] for r in records), UraDaily.day <= max(r['day'] for r in records)))}
        new_records, changed = [], []
        for record in records:
            old = existing.get((record['service_id'],record['day']))
            if old is None:
                new_records.append(record)
            elif any(getattr(old,key) != value for key,value in record.items()):
                if not replace:
                    raise ValueError(f"Conflito no serviço {record['service_id']} em {record['day']}: já existe outro conteúdo. Nenhum registro foi alterado. Para corrigir, marque a substituição e informe a justificativa.")
                changed.append((old, record))
        run = UraImportRun(project=project,source_name=source_name,sha256=hashlib.sha256(raw).hexdigest(),raw_csv=original,
                           username=username,record_count=len(records),inserted_count=len(new_records),
                           unchanged_count=len(records)-len(new_records)-len(changed),replaced_count=len(changed),reason=reason or None)
        db.add(run)
        db.flush()
        for record in new_records:
            db.add(UraDaily(project=project,import_id=run.id,**record))
        for old, record in changed:
            before = {key:getattr(old,key) for key in COUNTS}
            after = {key:record[key] for key in COUNTS}
            db.add(UraDailyRevision(import_id=run.id,previous_import_id=old.import_id,project=project,service_id=old.service_id,day=old.day,
                                    before=json.dumps(before,sort_keys=True),after=json.dumps(after,sort_keys=True)))
            for key,value in record.items():
                setattr(old,key,value)
            old.import_id = run.id
        db.add(AdminActionLog(username=username, action='import_ura_csv', table_name='ura_daily', status='success',
                              message=f'Importação URA {run.id}; projeto={project}; SHA256={run.sha256}; novos={run.inserted_count}; existentes={run.unchanged_count}; substituídos={run.replaced_count}'
                                      + (f'; justificativa={reason}' if changed else '') + '.'))
        db.commit()
        return run
    except Exception:
        db.rollback()
        raise
