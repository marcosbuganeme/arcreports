"""Cadastros que alimentam a apuração: feriados, ausências, pessoal, projetos, disponibilidade,
mapas de fatura e entidade, e a consulta à API 4Biz."""
import base64
import binascii
from datetime import date
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app import comunix
from app.billing_rules import Competence, contract_month, current_competence, portal_today
from app.contract_apuracao import INVOICE_CHOICES, INVOICES
from app.contract_registries import (PROFILES, REGISTRIES, confirm_registry, confirmed, import_absences, import_availability,
                                     import_projects, import_staff, reopen_registry)
from app.database import get_db
from app.models import (AdminActionLog, ContractAbsence, ContractAvailability, ContractEntityMap, ContractHoliday,
                        ContractInvoiceMap, ContractProjectScore, ContractRegistryStatus, ContractStaff, UraApiDaily)
from app.routes.common import form_data, render
from app.security import require_admin, verify_csrf
from app.ura import MAX_CSV_BYTES, normalize_project

router = APIRouter()
HOLIDAY_KINDS = {'nacional': 'Nacional', 'estadual': 'Estadual (GO)', 'municipal': 'Municipal (Goiânia)',
                 'ponto_facultativo': 'Ponto facultativo', 'feriado': 'Feriado do calendário do GLPI'}
IMPORTERS = {
    'ausencias': (lambda db, raw, period, user: import_absences(db, raw, period, user), ('ausencias_item01', 'ausencias_item02')),
    'pessoal': (lambda db, raw, period, user: import_staff(db, raw, user), ('pessoal',)),
    'projetos': (lambda db, raw, period, user: import_projects(db, raw, period, user), ('projetos',)),
    'disponibilidade': (lambda db, raw, period, user: import_availability(db, raw, period, user), ('disponibilidade',)),
}
LAYOUTS = {'ausencias': 'data;equipe;perfil;pessoa;coberta;observacao', 'pessoal': 'pessoa;equipe;perfil;inicio;fim;desligamento',
           'projetos': 'projeto;item;prazo;qualidade;comunicacao', 'disponibilidade': 'indicador;ativo;htp;hip;hmp;hit'}


def _period(value):
    period = Competence.parse(value) if value else current_competence()
    contract_month(period)
    return period


def _back(period, **extra):
    return RedirectResponse('/admin/contract/cadastros?' + urlencode({'competencia': period.label, **extra}), status_code=303)


def _raw(data):
    encoded = data.get('csv_base64') or ''
    if len(encoded) > (MAX_CSV_BYTES * 4 // 3 + 8):
        raise ValueError('O CSV deve ter no máximo 2 MB.')
    try:
        return base64.b64decode(encoded, validate=True) if encoded else (data.get('csv_content') or '').encode('utf-8')
    except binascii.Error as exc:
        raise ValueError('Arquivo inválido. Selecione o CSV novamente.') from exc


def registry_context(db, period, project):
    config = comunix.load_config(db)
    return {
        'active': 'contract', 'period': period, 'project': project, 'registries': REGISTRIES, 'layouts': LAYOUTS, 'profiles': PROFILES,
        'status': {key: confirmed(db, period, key) for key in REGISTRIES}, 'holiday_kinds': HOLIDAY_KINDS,
        'holidays': db.scalars(select(ContractHoliday).order_by(ContractHoliday.day)).all(),
        'absences': db.scalars(select(ContractAbsence).where(ContractAbsence.day >= period.start, ContractAbsence.day <= period.end)
                               .order_by(ContractAbsence.day, ContractAbsence.team)).all(),
        'staff': db.scalars(select(ContractStaff).order_by(ContractStaff.team, ContractStaff.person_ref)).all(),
        'projects': db.scalars(select(ContractProjectScore).where(ContractProjectScore.competence == period.label)).all(),
        'assets': db.scalars(select(ContractAvailability).where(ContractAvailability.competence == period.label)
                             .order_by(ContractAvailability.indicator, ContractAvailability.asset)).all(),
        'invoice_map': db.scalars(select(ContractInvoiceMap).order_by(ContractInvoiceMap.indicator)).all(),
        'entity_map': db.scalars(select(ContractEntityMap).order_by(ContractEntityMap.entity_id)).all(),
        'invoice_choices': INVOICE_CHOICES, 'invoices': INVOICES,
        'api': {k: v for k, v in config.items() if k != 'password'}, 'api_has_password': bool(config['password']),
        'api_rows': db.scalars(select(UraApiDaily).where(UraApiDaily.project == project, UraApiDaily.day >= period.start,
                                                         UraApiDaily.day <= period.end).order_by(UraApiDaily.day, UraApiDaily.service_name)).all(),
    }


def _fail(request, db, period, project, message, status_code=400):
    db.rollback()
    return render(request, 'contract_registry.html', {**registry_context(db, period, project), 'error': message}, db, status_code=status_code)


@router.get('/admin/contract/cadastros')
async def registry_page(request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    if isinstance(user, RedirectResponse):
        return user
    try:
        period = _period(request.query_params.get('competencia'))
        project = normalize_project(request.query_params.get('projeto') or 'DETRAN-GO')
    except ValueError as exc:
        return render(request, 'contract_registry.html', {'active': 'contract', 'error': str(exc)}, db, status_code=400)
    return render(request, 'contract_registry.html', {**registry_context(db, period, project),
                                                      'notice': request.query_params.get('ok')}, db)


@router.post('/admin/contract/cadastros/importar', dependencies=[Depends(verify_csrf)])
async def registry_import(request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    if isinstance(user, RedirectResponse):
        return user
    data = await form_data(request)
    period, project = current_competence(), 'DETRAN-GO'
    try:
        period = _period(data.get('competencia'))
        kind = data.get('cadastro') or ''
        if kind not in IMPORTERS:
            raise ValueError('Cadastro inválido.')
        importer, registries = IMPORTERS[kind]
        count = importer(db, _raw(data), period, user.username)
        for registry in registries:
            # O cadastro de pessoal vale para o contrato inteiro: reabre todas as competências confirmadas.
            scope = (ContractRegistryStatus.competence.isnot(None) if registry == 'pessoal'
                     else ContractRegistryStatus.competence == period.label)
            for status in db.scalars(select(ContractRegistryStatus).where(ContractRegistryStatus.registry == registry, scope)).all():
                reopen_registry(db, Competence.parse(status.competence), registry, user.username)
    except ValueError as exc:
        return _fail(request, db, period, project, str(exc))
    except SQLAlchemyError:
        return _fail(request, db, period, project, 'Falha ao gravar o cadastro. Nada deste arquivo foi salvo.', 409)
    return _back(period, ok=f'{count} linhas importadas em {kind}. Confira e confirme o cadastro como completo.')


@router.post('/admin/contract/cadastros/confirmar', dependencies=[Depends(verify_csrf)])
async def registry_confirm(request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    if isinstance(user, RedirectResponse):
        return user
    data = await form_data(request)
    period = current_competence()
    try:
        period = _period(data.get('competencia'))
        registry = data.get('cadastro') or ''
        if data.get('acao') == 'reabrir':
            reopen_registry(db, period, registry, user.username)
        else:
            confirm_registry(db, period, registry, user.username)
    except ValueError as exc:
        return _fail(request, db, period, 'DETRAN-GO', str(exc))
    return _back(period, ok='Situação do cadastro atualizada.')


@router.post('/admin/contract/cadastros/feriado', dependencies=[Depends(verify_csrf)])
async def registry_holiday(request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    if isinstance(user, RedirectResponse):
        return user
    data = await form_data(request)
    period = current_competence()
    try:
        period = _period(data.get('competencia'))
        try:
            day = date.fromisoformat(data.get('dia') or '')
        except ValueError as exc:
            raise ValueError('Data do feriado inválida.') from exc
        existing = db.get(ContractHoliday, day)
        if data.get('acao') == 'remover':
            if existing is None:
                raise ValueError('Feriado não cadastrado.')
            db.delete(existing)
            message = f'Feriado {day.isoformat()} removido ({existing.name}).'
        else:
            name, kind, source = (data.get('nome') or '').strip(), data.get('tipo') or '', (data.get('fonte') or '').strip()
            if not name or len(name) > 160 or kind not in HOLIDAY_KINDS or not source or len(source) > 160:
                raise ValueError('Informe nome, tipo e fonte do feriado (até 160 caracteres).')
            if existing:
                existing.name, existing.kind, existing.source, existing.updated_by = name, kind, source, user.username
            else:
                db.add(ContractHoliday(day=day, name=name, kind=kind, source=source, updated_by=user.username))
            message = f'Feriado {day.isoformat()} gravado: {name} ({kind}); fonte: {source}.'
        db.add(AdminActionLog(username=user.username, action='contract_holiday', table_name='contract_holidays',
                              status='success', message=message))
        db.commit()
    except ValueError as exc:
        return _fail(request, db, period, 'DETRAN-GO', str(exc))
    return _back(period, ok='Calendário atualizado.')


@router.post('/admin/contract/cadastros/fatura', dependencies=[Depends(verify_csrf)])
async def registry_invoice(request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    if isinstance(user, RedirectResponse):
        return user
    data = await form_data(request)
    period = current_competence()
    try:
        period = _period(data.get('competencia'))
        row = db.get(ContractInvoiceMap, data.get('indicador') or '')
        invoice = data.get('fatura') or ''
        source = (data.get('fonte') or '').strip()
        if row is None or invoice not in INVOICE_CHOICES:
            raise ValueError('Indicador ou fatura inválidos.')
        confirmed_flag = data.get('confirmado') == '1'
        if confirmed_flag and (invoice == 'a_confirmar' or len(source) < 10):
            raise ValueError('Para confirmar, escolha a fatura e registre a fonte da decisão (mensagem, ata ou ofício).')
        row.invoice, row.confirmed, row.updated_by = invoice, confirmed_flag, user.username
        row.source = (source or row.source)[:255]
        db.add(AdminActionLog(username=user.username, action='contract_invoice_map', table_name='contract_invoice_map', status='success',
                              message=f'{row.indicator} → {invoice}; confirmado={confirmed_flag}; fonte: {row.source}'))
        db.commit()
    except ValueError as exc:
        return _fail(request, db, period, 'DETRAN-GO', str(exc))
    return _back(period, ok='Mapa de faturas atualizado.')


@router.post('/admin/contract/cadastros/entidade', dependencies=[Depends(verify_csrf)])
async def registry_entity(request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    if isinstance(user, RedirectResponse):
        return user
    data = await form_data(request)
    period = current_competence()
    try:
        period = _period(data.get('competencia'))
        try:
            entity_id = int(data.get('entidade') or '')
        except ValueError as exc:
            raise ValueError('Entidade inválida.') from exc
        row = db.get(ContractEntityMap, entity_id)
        if data.get('acao') == 'remover':
            if row is None:
                raise ValueError('Entidade não mapeada.')
            db.delete(row)
            message = f'Entidade {entity_id} removida do mapa.'
        else:
            invoice, source = data.get('fatura') or '', (data.get('fonte') or '').strip()
            if invoice not in INVOICES or not source:
                raise ValueError('Escolha Item 01 ou Item 02 e registre a fonte.')
            confirmed_flag = data.get('confirmado') == '1'
            if row is None:
                row = ContractEntityMap(entity_id=entity_id, invoice=invoice, confirmed=confirmed_flag, source=source[:255], updated_by=user.username)
                db.add(row)
            else:
                row.invoice, row.confirmed, row.source, row.updated_by = invoice, confirmed_flag, source[:255], user.username
            message = f'Entidade {entity_id} → {invoice}; confirmado={confirmed_flag}; fonte: {source}'
        db.add(AdminActionLog(username=user.username, action='contract_entity_map', table_name='contract_entity_map',
                              status='success', message=message))
        db.commit()
    except ValueError as exc:
        return _fail(request, db, period, 'DETRAN-GO', str(exc))
    return _back(period, ok='Mapa de entidades atualizado.')


@router.post('/admin/contract/cadastros/api', dependencies=[Depends(verify_csrf)])
async def registry_api_config(request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    if isinstance(user, RedirectResponse):
        return user
    data = await form_data(request)
    period = current_competence()
    try:
        period = _period(data.get('competencia'))
        comunix.save_config(db, {'user': data.get('usuario'), 'password': data.get('senha'), 'login_url': data.get('login_url'),
                                 'query_url': data.get('consulta_url'), 'project': data.get('projeto_api')}, user.username)
    except ValueError as exc:
        return _fail(request, db, period, 'DETRAN-GO', str(exc))
    return _back(period, ok='Configuração da API gravada. A senha não é exibida nem registrada no log.')


@router.post('/admin/contract/cadastros/api/consultar', dependencies=[Depends(verify_csrf)])
async def registry_api_fetch(request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    if isinstance(user, RedirectResponse):
        return user
    data = await form_data(request)
    period, project = current_competence(), 'DETRAN-GO'
    try:
        period = _period(data.get('competencia'))
        project = normalize_project(data.get('projeto') or 'DETRAN-GO')
        start, end = period.start, min(period.end, portal_today())
        result = comunix.fetch(db, start, end, project, user.username)
    except ValueError as exc:
        return _fail(request, db, period, project, str(exc), 502)
    return _back(period, projeto=project, ok=f"API 4Biz: {result['calls']} ligações atendidas agregadas em {result['groups']} dias e serviços.")
