from __future__ import annotations

from datetime import date
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import PessoasProfissional, TrsOcorrencia, TrsTipo
from app.pessoas import parse_competencia
from app.routes.common import form_data, form_lists, render
from app.routes.pessoas import log_pessoas_action, parse_date
from app.security import require_admin, verify_csrf
from app.system_config import get_system_config_value, set_system_config_value
from app.trs import (
    AUTOMATICOS,
    CONFIG_CMDB,
    CONFIG_RELATORIO,
    FONTES,
    STATUS,
    UNIDADES,
    competencia_de,
    ensure_trs_tipos,
    montar_apuracao,
    notificacao_previa,
    pontos_brutos,
    sugestoes_competencia,
    _competencia_anterior,
)


router = APIRouter()


def trs_redirect(path: str, message: str, error: bool = False) -> RedirectResponse:
    key = "error" if error else "message"
    separator = "&" if "?" in path else "?"
    return RedirectResponse(f"{path}{separator}{key}={quote(message)}", status_code=status.HTTP_303_SEE_OTHER)


def competencia_param(request: Request) -> str:
    ano, mes = parse_competencia(request.query_params.get("competencia"))
    return f"{ano:04d}-{mes:02d}"


def contexto(request: Request, aba: str, **extra) -> dict:
    return {
        "active": "trs",
        "active_tab": aba,
        "message": request.query_params.get("message"),
        "error": request.query_params.get("error"),
        "unidades": UNIDADES,
        **extra,
    }


def parse_int(value: str | None, default: int = 1) -> int:
    try:
        return int((value or "").strip())
    except ValueError:
        return default


# ---------------------------------------------------------------- ocorrências


@router.get("/trs", response_class=HTMLResponse)
def trs_ocorrencias(request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    if isinstance(user, RedirectResponse):
        return user
    ensure_trs_tipos(db)
    competencia = competencia_param(request)
    filtro_status = request.query_params.get("status", "")
    query = db.query(TrsOcorrencia).filter(TrsOcorrencia.competencia == competencia)
    if filtro_status in STATUS:
        query = query.filter(TrsOcorrencia.status == filtro_status)
    ocorrencias = query.order_by(TrsOcorrencia.data_ocorrencia.desc(), TrsOcorrencia.id.desc()).all()
    tipos = db.query(TrsTipo).filter(TrsTipo.ativo.is_(True)).order_by(TrsTipo.ordem).all()
    tipos_por_codigo = {t.codigo: t for t in db.query(TrsTipo).all()}
    editar = request.query_params.get("editar")
    return render(
        request,
        "trs_ocorrencias.html",
        contexto(
            request,
            "ocorrencias",
            competencia=competencia,
            filtro_status=filtro_status,
            ocorrencias=[(oc, pontos_brutos(oc, tipos_por_codigo[oc.codigo])) for oc in ocorrencias],
            tipos=tipos,
            profissionais=db.query(PessoasProfissional).order_by(PessoasProfissional.nome).all(),
            status_lista=STATUS,
            fontes=FONTES,
            editar_id=int(editar) if editar and editar.isdigit() else None,
            hoje=date.today(),
        ),
        db,
    )


def validar_ocorrencia(db: Session, data: dict, ignorar_id: int | None = None):
    tipo = db.get(TrsTipo, (data.get("codigo") or "").strip())
    dia = parse_date(data.get("data_ocorrencia"))
    natureza = data.get("natureza") if data.get("natureza") in ("ocorrencia", "notificacao") else "ocorrencia"
    quantidade = parse_int(data.get("quantidade"), 1)
    evidencia = (data.get("evidencia") or "").strip()[:500]
    fonte = data.get("fonte") if data.get("fonte") in FONTES else FONTES[0]
    status_valor = data.get("status") if data.get("status") in STATUS else "Em aberto"
    profissional_id = int(data["profissional_id"]) if (data.get("profissional_id") or "").isdigit() else None
    if not tipo or not dia:
        return None, "Informe o TRS e a data da ocorrência."
    if not evidencia:
        return None, "Informe a evidência (número do chamado, ofício, notificação ou descrição do fato)."
    if quantidade < 1:
        return None, "A quantidade deve ser 1 ou mais."
    if natureza == "notificacao" and tipo.regra != "reincidencia":
        return None, f"{tipo.codigo} não depende de notificação prévia: registre como ocorrência."
    if tipo.regra == "reincidencia" and natureza == "ocorrencia" and not notificacao_previa(db, tipo.codigo, dia, ignorar_id):
        return None, (
            f"{tipo.codigo} só gera pontos após reincidência formalmente notificada. "
            "Registre primeiro a notificação (natureza \"Notificação\") com a data em que o DETRAN notificou."
        )
    return {
        "codigo": tipo.codigo,
        "natureza": natureza,
        "data_ocorrencia": dia,
        "competencia": competencia_de(dia),
        "quantidade": 1 if natureza == "notificacao" else quantidade,
        "evidencia": evidencia,
        "fonte": fonte,
        "status": status_valor,
        "profissional_id": profissional_id,
        "justificativa": (data.get("justificativa") or "").strip() or None,
    }, None


@router.post("/trs", dependencies=[Depends(verify_csrf)])
async def trs_ocorrencia_create(request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    if isinstance(user, RedirectResponse):
        return user
    ensure_trs_tipos(db)
    valores, erro = validar_ocorrencia(db, await form_data(request))
    if erro:
        return trs_redirect("/trs", erro, True)
    ocorrencia = TrsOcorrencia(**valores, origem="manual", registrado_por=user.username)
    db.add(ocorrencia)
    db.commit()
    log_pessoas_action(db, user, "trs_ocorrencia_create", "trs_ocorrencias", f"{ocorrencia.codigo} #{ocorrencia.id} registrado.")
    return trs_redirect(f"/trs?competencia={ocorrencia.competencia}", f"{ocorrencia.codigo} registrado.")


@router.post("/trs/{ocorrencia_id:int}", dependencies=[Depends(verify_csrf)])
async def trs_ocorrencia_update(ocorrencia_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    if isinstance(user, RedirectResponse):
        return user
    ocorrencia = db.get(TrsOcorrencia, ocorrencia_id)
    if not ocorrencia:
        return trs_redirect("/trs", "Ocorrência não encontrada.", True)
    data = await form_data(request)
    data.setdefault("codigo", ocorrencia.codigo)
    valores, erro = validar_ocorrencia(db, data, ocorrencia.id)
    if erro:
        return trs_redirect(f"/trs?competencia={ocorrencia.competencia}&editar={ocorrencia.id}", erro, True)
    if valores["status"] in ("Contestada", "Cancelada") and not valores["justificativa"]:
        return trs_redirect(
            f"/trs?competencia={ocorrencia.competencia}&editar={ocorrencia.id}",
            "Para contestar ou cancelar, informe a justificativa (TR 8.28.4.19 e 8.28.4.20).",
            True,
        )
    anterior = ocorrencia.status
    for campo, valor in valores.items():
        setattr(ocorrencia, campo, valor)
    db.commit()
    log_pessoas_action(
        db, user, "trs_ocorrencia_update", "trs_ocorrencias",
        f"{ocorrencia.codigo} #{ocorrencia.id} atualizado (status {anterior} -> {ocorrencia.status}).",
    )
    return trs_redirect(f"/trs?competencia={ocorrencia.competencia}", f"{ocorrencia.codigo} atualizado.")


@router.post("/trs/{ocorrencia_id:int}/delete", dependencies=[Depends(verify_csrf)])
def trs_ocorrencia_delete(ocorrencia_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    if isinstance(user, RedirectResponse):
        return user
    ocorrencia = db.get(TrsOcorrencia, ocorrencia_id)
    if not ocorrencia:
        return trs_redirect("/trs", "Ocorrência não encontrada.", True)
    destino = f"/trs?competencia={ocorrencia.competencia}"
    descricao = f"{ocorrencia.codigo} #{ocorrencia.id} ({ocorrencia.data_ocorrencia:%d/%m/%Y}) excluído."
    db.delete(ocorrencia)
    db.commit()
    log_pessoas_action(db, user, "trs_ocorrencia_delete", "trs_ocorrencias", descricao)
    return trs_redirect(destino, "Registro excluído.")


# ---------------------------------------------------------------- sugestões


@router.get("/trs/sugestoes", response_class=HTMLResponse)
def trs_sugestoes(request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    if isinstance(user, RedirectResponse):
        return user
    ensure_trs_tipos(db)
    competencia = competencia_param(request)
    sugestoes, avisos = sugestoes_competencia(db, competencia)
    tipos = {t.codigo: t for t in db.query(TrsTipo).all()}
    return render(
        request,
        "trs_sugestoes.html",
        contexto(request, "sugestoes", competencia=competencia, sugestoes=sugestoes, avisos=avisos, tipos=tipos,
                 automaticos=sorted(AUTOMATICOS, key=lambda c: tipos[c].ordem if c in tipos else 0)),
        db,
    )


@router.post("/trs/sugestoes", dependencies=[Depends(verify_csrf)])
async def trs_sugestoes_registrar(request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    if isinstance(user, RedirectResponse):
        return user
    listas = await form_lists(request)
    competencia = (listas.get("competencia") or [""])[0]
    ano, mes = parse_competencia(competencia)
    competencia = f"{ano:04d}-{mes:02d}"
    escolhidas = set(listas.get("chave", []))
    if not escolhidas:
        return trs_redirect(f"/trs/sugestoes?competencia={competencia}", "Selecione ao menos uma sugestão.", True)
    sugestoes, _ = sugestoes_competencia(db, competencia)
    criadas = 0
    for s in sugestoes:
        if s["chave"] not in escolhidas:
            continue
        db.add(TrsOcorrencia(
            codigo=s["codigo"], natureza="ocorrencia", data_ocorrencia=s["data"], competencia=competencia_de(s["data"]),
            quantidade=s["quantidade"], evidencia=s["evidencia"], profissional_id=s["profissional_id"],
            fonte="Detecção automática", status="Em aberto", origem="automatico", chave=s["chave"],
            registrado_por=user.username,
        ))
        criadas += 1
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return trs_redirect(f"/trs/sugestoes?competencia={competencia}", "Algumas sugestões já tinham sido registradas. Atualize a página.", True)
    log_pessoas_action(db, user, "trs_sugestoes_registrar", "trs_ocorrencias", f"{criadas} sugestão(ões) de {competencia} registrada(s).")
    return trs_redirect(f"/trs?competencia={competencia}", f"{criadas} ocorrência(s) registrada(s) a partir das sugestões.")


# ---------------------------------------------------------------- apuração


@router.get("/trs/apuracao", response_class=HTMLResponse)
def trs_apuracao(request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    if isinstance(user, RedirectResponse):
        return user
    ensure_trs_tipos(db)
    competencia = competencia_param(request)
    relatorio = _competencia_anterior(competencia)
    return render(
        request,
        "trs_apuracao.html",
        contexto(
            request,
            "apuracao",
            **montar_apuracao(db, competencia),
            relatorio_competencia=relatorio,
            relatorio_entregue=get_system_config_value(db, CONFIG_RELATORIO.format(competencia=relatorio)) or "",
            cmdb_implantado=get_system_config_value(db, CONFIG_CMDB) or "",
        ),
        db,
    )


@router.post("/trs/controles", dependencies=[Depends(verify_csrf)])
async def trs_controles(request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    if isinstance(user, RedirectResponse):
        return user
    data = await form_data(request)
    ano, mes = parse_competencia(data.get("competencia"))
    competencia = f"{ano:04d}-{mes:02d}"
    relatorio = _competencia_anterior(competencia)
    entregue = parse_date(data.get("relatorio_entregue"))
    cmdb = parse_date(data.get("cmdb_implantado"))
    set_system_config_value(db, CONFIG_RELATORIO.format(competencia=relatorio), entregue.isoformat() if entregue else "")
    set_system_config_value(db, CONFIG_CMDB, cmdb.isoformat() if cmdb else "")
    log_pessoas_action(db, user, "trs_controles", "system_config",
                       f"Relatório de {relatorio} entregue em {entregue or '-'}; CMDB implantado em {cmdb or '-'}.")
    return trs_redirect(f"/trs/apuracao?competencia={competencia}", "Datas de controle salvas.")


# ---------------------------------------------------------------- catálogo


@router.get("/trs/catalogo", response_class=HTMLResponse)
def trs_catalogo(request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    if isinstance(user, RedirectResponse):
        return user
    ensure_trs_tipos(db)
    return render(
        request,
        "trs_catalogo.html",
        contexto(request, "catalogo", competencia=competencia_param(request),
                 tipos=db.query(TrsTipo).order_by(TrsTipo.ordem).all(), automaticos=AUTOMATICOS),
        db,
    )


@router.post("/trs/catalogo/{codigo}", dependencies=[Depends(verify_csrf)])
async def trs_catalogo_update(codigo: str, request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    if isinstance(user, RedirectResponse):
        return user
    tipo = db.get(TrsTipo, codigo)
    if not tipo:
        return trs_redirect("/trs/catalogo", "TRS não encontrado.", True)
    data = await form_data(request)
    pontos = parse_int(data.get("pontos"), -1)
    if pontos < 0:
        return trs_redirect("/trs/catalogo", "Pontuação inválida.", True)
    anterior = tipo.pontos
    tipo.pontos = pontos
    tipo.ativo = data.get("ativo") == "on"
    db.commit()
    log_pessoas_action(db, user, "trs_catalogo_update", "trs_tipos", f"{codigo}: pontos {anterior} -> {pontos}, ativo={tipo.ativo}.")
    return trs_redirect("/trs/catalogo", f"{codigo} atualizado.")
