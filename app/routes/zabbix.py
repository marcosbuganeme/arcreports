from __future__ import annotations

from datetime import date
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.pessoas import parse_competencia
from app.routes.common import form_data, render
from app.routes.pessoas import log_pessoas_action
from app.security import require_admin, verify_csrf
from app.zabbix_ind import ZabbixErro, coletar, config, resumo_mes, salvar_config, testar


router = APIRouter()


def zabbix_redirect(message: str, error: bool = False, extra: str = "") -> RedirectResponse:
    key = "error" if error else "message"
    return RedirectResponse(f"/admin/zabbix?{key}={quote(message)}{extra}", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/admin/zabbix", response_class=HTMLResponse)
def zabbix_pagina(request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    if isinstance(user, RedirectResponse):
        return user
    ano, mes = parse_competencia(request.query_params.get("competencia"))
    competencia = f"{ano:04d}-{mes:02d}"
    teste = None
    if request.query_params.get("testar") == "1":
        try:
            teste = testar(db)
        except ZabbixErro as exc:
            teste = {"erro": str(exc)}
    return render(
        request,
        "admin_zabbix.html",
        {
            "active": "zabbix",
            "message": request.query_params.get("message"),
            "error": request.query_params.get("error"),
            "cfg": config(db),
            "competencia": competencia,
            "resumo": resumo_mes(db, competencia),
            "teste": teste,
            "hoje": date.today(),
        },
        db,
    )


@router.post("/admin/zabbix", dependencies=[Depends(verify_csrf)])
async def zabbix_salvar(request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    if isinstance(user, RedirectResponse):
        return user
    data = await form_data(request)
    url = (data.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        return zabbix_redirect("Informe a URL do Zabbix começando com http:// ou https://.", True)
    for campo in ("sla_ind01", "sla_ind02"):
        if (data.get(campo) or "").strip() and not data[campo].strip().isdigit():
            return zabbix_redirect("O ID do SLA deve ser numérico.", True)
    salvar_config(db, url, (data.get("token") or "").strip() or None, data.get("verificar") == "on",
                  data.get("sla_ind01") or "", data.get("sla_ind02") or "")
    log_pessoas_action(db, user, "zabbix_config", "system_config", "Configuração da integração Zabbix salva (token não registrado).")
    return zabbix_redirect("Configuração salva.", extra="&testar=1")


@router.post("/admin/zabbix/coletar", dependencies=[Depends(verify_csrf)])
async def zabbix_coletar(request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    if isinstance(user, RedirectResponse):
        return user
    data = await form_data(request)
    completo = data.get("completo") == "on"
    try:
        resumo = coletar(db, completo=completo)
    except ZabbixErro as exc:
        db.rollback()
        return zabbix_redirect(str(exc), True)
    texto = "; ".join(f"{k}: {v} linhas" if isinstance(v, int) else f"{k}: {v}" for k, v in resumo.items())
    log_pessoas_action(db, user, "zabbix_coleta", "zabbix_sli", f"Coleta manual ({'completa' if completo else 'incremental'}): {texto}.")
    return zabbix_redirect(f"Coleta concluída. {texto}.")
