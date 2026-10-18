"""Apuração contratual administrativa, separada dos relatórios SQL instalados."""
from datetime import date

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.billing_rules import Competence, contract_month, pao_factor, decimal_value, adjusted_goal, weighted_points, knowledge_goal
from app.database import get_db
from app.routes.common import render
from app.security import require_admin

router = APIRouter()


def contract_context(params):
    period = Competence.parse(params.get('competencia') or Competence.containing(date.today()).label)
    base_goal = decimal_value(params.get('meta_base') or '95')
    direction = params.get('direcao') or 'minimum'
    base_points = decimal_value(params.get('pontos_base') or '0')
    return {'period': period, 'contract_month': contract_month(period), 'factor': pao_factor(period),
            'knowledge_goal': knowledge_goal(period), 'trimester': (contract_month(period) - 1) // 3 + 1,
            'base_points': base_points, 'weighted_points': weighted_points(base_points, period),
            'base_goal': base_goal, 'direction': direction, 'goal': adjusted_goal(base_goal, direction, period)}


@router.get('/admin/contract')
async def contract_page(request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    if isinstance(user, RedirectResponse):
        return user
    try:
        context = contract_context(request.query_params)
    except ValueError as exc:
        return render(request, 'contract.html', {'active': 'contract', 'error': str(exc)}, db, status_code=400)
    return render(request, 'contract.html', {'active': 'contract', **context}, db)
