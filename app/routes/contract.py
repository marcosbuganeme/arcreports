"""Apuração contratual administrativa, separada dos relatórios SQL instalados."""
import base64
import binascii
from datetime import date
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy import select

from app.billing_rules import Competence, contract_month, pao_factor, decimal_value, adjusted_goal, weighted_points, knowledge_goal, staffing_window
from app.database import get_db
from app.models import UraImportRun
from app.ura import import_ura_csv, normalize_project, MAX_CSV_BYTES
from app.routes.common import render, form_data
from app.security import require_admin, verify_csrf

router = APIRouter()


def contract_context(params):
    period = Competence.parse(params.get('competencia') or Competence.containing(date.today()).label)
    base_goal = decimal_value(params.get('meta_base') or '95')
    direction = params.get('direcao') or 'minimum'
    base_points = decimal_value(params.get('pontos_base') or '0')
    return {'period': period, 'contract_month': contract_month(period), 'factor': pao_factor(period),
            'staffing_window': staffing_window(period), 'knowledge_goal': knowledge_goal(period), 'trimester': (contract_month(period) - 1) // 3 + 1,
            'base_points': base_points, 'weighted_points': weighted_points(base_points, period),
            'base_goal': base_goal, 'direction': direction, 'goal': adjusted_goal(base_goal, direction, period)}


@router.get('/admin/contract')
async def contract_page(request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    if isinstance(user, RedirectResponse):
        return user
    try:
        context = contract_context(request.query_params)
        context['project'] = normalize_project(request.query_params.get('projeto') or 'DETRAN-GO')
    except ValueError as exc:
        return render(request, 'contract.html', {'active': 'contract', 'error': str(exc)}, db, status_code=400)
    context['imports'] = db.scalars(select(UraImportRun).where(UraImportRun.project == context['project']).order_by(UraImportRun.id.desc()).limit(20)).all()
    return render(request, 'contract.html', {'active': 'contract', **context}, db)


@router.post('/admin/contract/ura', dependencies=[Depends(verify_csrf)])
async def upload_ura(request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    if isinstance(user, RedirectResponse):
        return user
    data = await form_data(request)
    try:
        period = Competence.parse(data.get('competencia') or Competence.containing(date.today()).label)
        contract_month(period)
        project = normalize_project(data.get('projeto'))
        encoded = data.get('csv_base64') or ''
        if len(encoded) > (MAX_CSV_BYTES * 4 // 3 + 8):
            raise ValueError('O CSV deve ter no máximo 2 MB.')
        try:
            raw = base64.b64decode(encoded, validate=True) if encoded else data.get('csv_content','').encode('utf-8')
        except binascii.Error as exc:
            raise ValueError('Arquivo inválido. Selecione o CSV novamente.') from exc
        run = import_ura_csv(db, raw, project=project,
                             source_name=data.get('source_name') or 'ura.csv', username=user.username)
    except ValueError as exc:
        return render(request, 'contract.html', {'active':'contract', 'error':str(exc)}, db, status_code=400)
    except SQLAlchemyError:
        db.rollback()
        return render(request, 'contract.html', {'active':'contract', 'error':'Falha ao gravar a importação. Nenhum registro deste arquivo foi salvo. Confira o banco local e tente novamente.'}, db, status_code=409)
    query = urlencode({'competencia':period.label, 'projeto':project, 'importacao':run.id})
    return RedirectResponse('/admin/contract?' + query, status_code=303)
